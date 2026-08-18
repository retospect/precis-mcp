---
status: draft
title: Multiuser access — tailscale-authenticated users, admin/user roles, per-doc visibility
prio: normal
model: opus
blocked-by: user-identity-and-ask-routing
---

# Multiuser access — tailscale-authenticated users, admin/user roles, per-doc visibility

Compiled from the 2026-08-18 design discussion (Reto + agent). Friendly-
academic threat model now (accidental leakage, not malice); designed so the
hardening step (RLS) is a transcription, not a redesign.

## Motivation / why

Precis is single-tenant by construction: one pg role (`agent_rw`) behind a
transaction-mode pgbouncer, no `users`/ACL tables, no per-request identity
anywhere (`refs.meta.owner` is null on all ~35 915 prod refs), an
unauthenticated web UI, and a process-wide `PRECIS_SOURCE` env var as the
sole caller signal. Reto wants a small academic group on the instance: an
**admin** who sees everything, and **users** who see only documents they
made or that were shared with them.

Three principles decided in discussion:

1. **Authn ≠ authz ≠ enforcement.** Tailscale supplies authentication and
   coarse network gating only; all document-level authorization is ours.
2. **Split the world by kind before splitting it by owner.** Fetched/
   ingested kinds (paper, patent, web, wikipedia, …) are a shared
   literature **commons** — a feature for collaborators, and it shrinks
   the enforcement surface to the personal kinds.
3. **Authority follows the provenance of the task, not the process.**
   System workers run wide; user-originated work runs as the user; jobs a
   user originated drop to that user's visibility ("sudo down, never up").

## Design

### Identity & authentication (Tailscale does this part)

- **MCP stdio + CLI over Tailscale SSH**: tailnet ACLs' `ssh` section maps
  tailnet identities → per-human unix accounts; each human runs their own
  `precis serve`. Process-level identity is then correct:
  `PRECIS_USER=<login>` resolved at startup against the `users` table.
- **Web UI**: bind loopback, front with `tailscale serve`; a small FastAPI
  middleware reads the verified `Tailscale-User-Login` header (tailnet-
  internal only — verify current header behavior at spec time; fallback:
  LocalAPI `whois` on the connecting IP) → per-request viewer. Subsumes
  `factory-post-auth.md`'s gap. Until it lands, tailnet ACLs keep the web
  port admin-only.
- **NOT from Tailscale**: sharing semantics, admin flag, Discord identity,
  worker identity.

### Data model

- `users` (login, `is_admin`, created_at). Admin is a flag, not a separate
  authority tier — the visibility filter branches on it.
- `refs.owner_id` (nullable FK; **null = commons/legacy**). Backfill: all
  existing refs → Reto (`elmsfeuer`).
- `shares` (ref_id, user_id; read/write bit deferred).
- `KindSpec.visibility: "commons" | "private"` in `src/precis/protocol.py`
  — initial values derived from the existing partition:
  `CacheBackedHandler` subclasses + `role="corpus"` ⇒ commons;
  `note_like` / `NumericRefHandler` family (draft, memory, todo, conv,
  gripe, quest, finding, …) ⇒ private.
- Do **not** build ACL on `user:<name>` tags — tags are visible, mutable
  metadata; they stay a routing/addressee axis only.

### Enforcement (code now, DB backstop later)

Per-human pg roles were considered and **rejected**: GRANTs are table-
granular so they express nothing row-level; they multiply pgbouncer pools;
the web daemon serves many humans over one pool; and schema reconcile
already drops GRANTs (`schema-reconcile-acls.md`, P0 — fix before adding
ACL tables). End state is **two or three pg roles by authority tier**
(RLS-enforced app role, system/bypass role, existing `agent_ro`), never
per human.

- **Phase 1 — app-level.** One module owns the semantics:
  `build_visibility_filter(viewer)` beside
  `src/precis/store/_tag_filter.py::build_tag_filter` (same
  `(sql_fragment, params)`-into-`clauses` pattern, uniform `r` alias),
  expressing: commons kind OR `owner_id = viewer` OR shared-with-viewer
  OR viewer.is_admin. Injected at the user-reachable read sites — the
  search/get family in `_blocks_ops.py` + `_refs_ops.py` (semantic search
  is pgvector **in Postgres**, so the same WHERE covers it; no external
  vector store). Mirror **write check** (owner-or-shared-write-or-admin)
  on `put`/`edit`/`delete`/`tag` at the verb layer
  (`src/precis/tools/core.py::_dispatch`, which already audit-logs per
  call). Viewer context threads per-request; identity rides
  **per-transaction** as `SET LOCAL app.viewer_id = …` — never a session
  GUC/`SET ROLE`: pgbouncer runs transaction pooling and session state
  leaks across tenants (the exact hazard `store/pool.py` documents).
- **Phase 2 — RLS backstop (harden later).** Policies read
  `current_setting('app.viewer_id')` (GUC-in-policy precedent:
  `0059_secrets_vault.sql`). Because phase 1 already threads identity
  per-transaction and states policy as owner-or-shared-or-admin, the RLS
  policy transcribes the Python fragment; a missed query path becomes an
  empty result instead of a leak. Gotcha: table owners bypass RLS — the
  enforced role must not own the tables (or `FORCE ROW LEVEL SECURITY`).

### Principals — root workers vs user-originated agents

Extend the `PRECIS_SOURCE` owner-vs-worker classification
(`handlers/_todo_guards.py`) into a structured principal
(`authority: system|user`, `user: <login|null>`):

1. **System workers, mechanical passes** (embed, classify, nursery, chunk
   synthesis): full visibility on the bypass role — necessary (embedding
   must see everyone's chunks). Load-bearing rule: **derived artifacts
   inherit the source ref's `owner_id`** — otherwise workers launder
   private docs into commons via summaries/cards.
2. **User-originated agents** (session MCP, web ask, asa reply): run *as
   the user* — reads filtered to their visibility, writes owned by them.
   LLM context assembly is the biggest real leak channel in this threat
   model; this is the case that closes it.
3. **User-originated background jobs** (dispatched todos, quest ticks):
   the todo/job row records `owner_id`; the claiming worker sets that
   user's viewer context for the job's duration. Sudo down, never up.
   This is the piece with real design friction (which passes are
   mechanical-system vs on-behalf-of; mixed batches) — **prototype it
   first**.

## In scope

- `users` / `shares` tables + `refs.owner_id` + backfill migration.
- `KindSpec.visibility` flag + initial per-kind values.
- `build_visibility_filter` + injection at user-reachable read sites;
  write-authorization check at verb dispatch.
- Viewer-context plumbing: `PRECIS_USER` for SSH/stdio/CLI; tailscale-serve
  header middleware for `precis_web` (closes `factory-post-auth.md`).
- Principal model incl. per-job on-behalf-of in the worker claim loop.
- Derived-artifact owner inheritance in synthesis passes.

## Explicitly NOT in scope

- **RLS itself** (phase 2, separate item when hardening is wanted) — but
  phase 1 must not foreclose it (per-transaction identity only).
- **Per-human pg roles / per-user DSNs** — rejected, see above.
- **Discord/asa per-user mapping** (today: channel allowlist = owner-
  equivalent reach; `author_handle` is free text). Deferred; interim
  stance for asa is a decision below.
- **Per-user bearer tokens for the network MCP transports**
  (`sse`/`streamable-http` single shared token) — unused in prod.
- OIDC / non-tailnet auth, Funnel/public exposure, share-links,
  groups/orgs, read-vs-write share bits.
- Rewriting the 905 scattered SQL sites — workers keep full visibility by
  design; only user-reachable paths are gated in phase 1.

## Acceptance criteria

- A non-admin user over Tailscale SSH: `search`/`get` return only commons
  + own + shared-with-them refs (incl. semantic/fused search); `get` by
  id of another user's private ref is denied/absent; `edit`/`delete`/`tag`
  on it refused. Admin sees and can do all.
- Web: requests without a resolvable tailnet identity are rejected (or
  admin-only per config); with one, the same visibility holds on drive/
  search/ask routes; `/factory` POSTs require admin.
- A summary/card synthesized from a private ref carries that ref's
  `owner_id` and is invisible to other non-admin users.
- A todo owned by user A, executed by the melchior worker, produces
  reads/writes scoped to A (verifiable via the job transcript / audit
  log).
- No session-level `SET ROLE`/GUC on any pgbouncer DSN (guard or test).
- Fresh single-user install stays zero-config: no `users` rows ⇒ implicit
  admin owner, filter short-circuits (no behavior change).
- Gate green (ruff/mypy/pytest); migration forward-only.

## Target + blast radius

- Schema: new tables `users`, `shares`; `refs.owner_id` (+ backfill).
- `src/precis/protocol.py` (`KindSpec`), `store/_tag_filter.py` sibling
  module, `store/_blocks_ops.py`, `store/_refs_ops.py`,
  `tools/core.py::_dispatch`, `handlers/_todo_guards.py`,
  `runtime/factory.py` (viewer context), `store/pool.py` (per-tx GUC).
- `precis_web/app.py` (+middleware), `precis/cli/web.py`, worker claim
  loop (`workers/`), synthesis passes (owner inheritance).
- Deploy: tailscale-serve config for web; per-human unix accounts +
  tailnet SSH ACLs (out-of-tree ansible).

## Open questions / decisions log

- **DECIDED**: commons-vs-private is a *kind-level* flag first; ownership
  applies within private kinds. Papers/caches are shared library.
- **DECIDED**: enforcement = app-level filter phase 1, RLS phase 2;
  identity per-transaction GUC; no per-human pg roles.
- **DECIDED**: derived artifacts inherit source owner; workers are
  system-authority; user-originated jobs run on-behalf-of.
- **OPEN — sharing granularity**: per-ref `shares` first; folder-level
  ("share this project") as sugar later? (Lean: yes, per-ref first.)
- **OPEN — web in round one?** If collaborators can live in MCP/CLI over
  SSH initially, web stays admin-only via tailnet ACL and the middleware
  ships in a follow-up — meaningfully smaller MVP.
- **OPEN — todos/quests under the factory**: one user's quest output —
  visible to others or scoped to the originator? (Interacts with the
  on-behalf-of prototype.)
- **OPEN — asa interim stance**: leave channel-allowlist =
  owner-equivalent (status quo, documented), or demote unmapped Discord
  users to commons-only reads until the mapping table lands?
- **OPEN — canonical prerequisite**: `user-identity-and-ask-routing.md`
  is partially shipped (`PrecisConfig.owner`, `ASKER`); fold its
  remainder into this item or ship it first as-is? (blocked-by set
  provisionally.)
