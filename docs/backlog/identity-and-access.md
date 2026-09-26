---
status: draft
prio: normal
---

# Identity and access

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## User identity & ask-user routing — design plan

_Grouped 2026-09-26; was `user-identity-and-ask-routing`._

- **Status**: proposed (2026-06-19).
- **Authors**: Reto + agent
- **Motivation**: "reto" is hard-coded as *the* human in several
  places, while the live DB refers to the same person as
  `user:elmsfeuer` and the code's own generalized path calls them
  `owner`. Pick one canonical identity, de-hard-code "reto", and make
  `ask-user` route to a named user instead of an implicit single one.

### 1. Problem

The system has a **single-user assumption** baked in under three
different names for the same human:

| Name | Where | Kind |
|------|-------|------|
| `reto` | `precis_web/ask.py` `ASKER = "reto"`; `web:reto` source + `waiting:reto` in design docs; a bare `reto` tag (1 ref in prod) | hard-coded literal |
| `elmsfeuer` | `user:elmsfeuer` tag in prod (44 refs); a migration comment in `0011_ref_level_decay.sql` | live data convention |
| `owner` | `precis_web/config.py` `DEFAULT_SOURCE = "web:owner"`; `web:*`→owner classification in `_todo_guards`; `meta.owner` (present in schema, **null on all 35 915 prod refs**) | the already-generalized concept |

Meanwhile the bot is `asa` (`user:asa`, 1308 refs; `ANSWERER = "asa"`;
the `asa-*` worker-source convention). `asa` is consistent — it is
only the *human* identity that is fragmented.

Two distinct problems fall out:

1. **Identity fragmentation.** Nothing ties `reto` ≡ `elmsfeuer` ≡
   `owner` together. A routing query on `user:elmsfeuer` misses
   anything stamped `reto`, and vice-versa.
2. **`ask-user` has no addressee.** `ask-user` / `ask-user:<text>`
   means "a human should answer," but not *which* human. Today that
   is fine (one human) — but the `user:<username>` axis already
   exists in the data, so multi-user routing is one binding away.

This doc proposes a canonical identity, the `ask-user`→`user:<who>`
binding, and cleans up the fragmentation. It also folds in the
already-agreed removal of the deprecated `asking-reto` alias.

### 2. Audit — every "reto" instance and its disposition

Generated 2026-06-19 by `grep -rinE '\breto\b'` over the tree +
`ILIKE '%reto%'` over prod tags/actors.

#### 2a. Hard-coded in code — **FIX**

| Site | Current | Proposed |
|------|---------|----------|
| `precis_web/ask.py:41` | `ASKER = "reto"` | resolve from config — `cfg.owner` (default below) |
| `precis_web/ask.py:44` | `ANSWERER = "asa"` | leave (bot identity is stable) or move to config for symmetry |

#### 2b. The `asking-reto` legacy alias — **REMOVE** (data-safe)

Already a deprecated alias for `ask-user`; prod carries **zero**
`asking-reto` rows, so removal needs no data migration. Sites
(comments / SQL / enum):

- `handlers/_todo_views.py` (≈8 sites: the `("asking-reto","asking-reto:")`
  prefix tuple, the `render_asking_reto` matcher, 3 `OR value LIKE
  'asking-reto:%'` clauses, doc comments)
- `handlers/todo.py` (`TodoView.ASKING_RETO` enum member + its
  dispatch row + the docstring)
- `workers/nursery.py`, `workers/executors/coordinator.py`,
  `workers/executors/_common.py`, `workers/dispatch.py` (comments + 1
  SQL clause in nursery)
- `precis_web/routes/asks.py`, `routes/todo.py` (prefix-strip +
  matchers)
- `data/skills/precis-todo-tree-help.md` (the "legacy alias" notes)

Keep `ask-user` / `ask-user:` exactly as-is. Removing `view='asking-reto'`
is a breaking change for any caller still passing it → CHANGELOG note.

#### 2c. Design-doc narrative "Reto" — **LEAVE** (historical intent)

`docs/backlog/todo-tree-plan.md`, `precis-web-build.md`, `storage-v2.md`
use "Reto" as the human actor in prose ("what Reto asked for",
`level:strategic | Reto`). These are point-in-time design records;
rewriting them rewrites history. **Exception:** literal config values
that drifted from the code — the docs say `source='web:reto'` /
`waiting:reto` while the code already uses `web:owner`. Add a one-line
note to those docs pointing at the generalized `web:owner` rather than
editing the narrative.

#### 2d. Author attribution — **LEAVE**

`pyproject.toml` `authors = [{ name = "Reto Stamm", email =
"reto@retostamm.com" }]`, `git` Co-Authored trailers, LICENSE. These
are correct and must not change.

#### 2e. Prod data — **DECIDE** (see §5)

| Datum | Count | Disposition |
|-------|------:|-------------|
| `user:elmsfeuer` | 44 | the live human identity — candidate canonical name |
| bare `reto` tag | 1 | normalize to `user:<canonical>` or drop |
| `project:reto-nanocompute`, `project:reto-catalyst-latent` | 1 each | project *names*, not identity — leave unless Reto wants a rename |
| `meta.owner` | null × 35 915 | unused; do not start populating without a reason |

### 3. Canonical identity

**Proposal: one configurable owner identity, surfaced as
`user:<owner>`.**

- New config field `PrecisConfig.owner` (env `PRECIS_OWNER`), the
  canonical username for the human running this instance. It feeds:
  - `ask.py` `ASKER` (replaces `"reto"`),
  - the `user:<owner>` tag the web/planner stamps when a todo asks
    "the owner" specifically,
  - any "this is the human" default.
- **Default value.** Recommend `owner` (matches the existing
  `web:owner` source convention and reads correctly on a fresh
  install). Reto's instance sets `PRECIS_OWNER=elmsfeuer` (or whatever
  canonical handle he wants — see open questions) so it lines up with
  the 44 existing `user:elmsfeuer` rows.
- This keeps the single-user case zero-config while making the
  identity *named and overridable* — the prerequisite for multi-user.

Rejected alternatives: hard-coding `elmsfeuer` (same bug, different
string); populating `meta.owner` per-ref (heavy, and routing wants a
tag axis, not a scalar column).

### 4. `ask-user` ↔ `user:<who>` binding

Today `ask-user` is an open pause tag whose **value carries the full
question prose** (verified in prod — 200-word values like "two
blockers on this todo …"). Two changes:

1. **Address the ask.** When something parks a todo on a human, it
   stamps both `ask-user` (the pause marker, unchanged semantics) and
   `user:<who>` (the addressee, defaulting to `cfg.owner`). Routing —
   the Discord/chatter preamble, `view='attention'`, the web Asks page
   — filters by the viewer's `user:<me>`, so each human sees only
   their own asks. With one user this is a no-op; with several it Just
   Works.
2. **Get the prose out of the tag value.** A tag is an index key, not
   a document. The question text should live in a chunk/comment on the
   todo (searchable, embeddable) with the tag reduced to a marker +
   optional short ref. `precis_web/routes/asks.py` already strips the
   `ask-user:` prefix to recover the text — that logic moves to
   reading the chunk instead.

#### Registry question

Should `user:<username>` validate against a registry?

- **Today:** open tag, typo-prone (`user:asa`, `user:elmsfeuer`
  coexist with no schema).
- **Option A (recommended, light):** a closed-vocab axis seeded from
  config (`PRECIS_OWNER` + a known bots list incl. `asa`). Rejects
  unknown `user:` values at `tag()` time.
- **Option B (heavier):** extend the `actors` table (today: `agent`,
  `chase`, `system`, `user`) with per-human rows and FK the tag. More
  infra than current needs justify; revisit if real multi-human
  routing lands.

### 5. Migration plan

- **No migration for `asking-reto`** (zero prod rows).
- **`user:elmsfeuer`:** the canonical handle is `elmsfeuer`, so the 44
  rows already match — **nothing to migrate**.
- **Bare `reto` tag (1 row):** a tiny forward migration renames it to
  `user:elmsfeuer` (or just drops it). This is the only DB write the
  whole change needs.
- **`config.owner`** is a new env var; absent → default `owner`. No
  schema change.

### 6. Implementation steps

1. Land this doc; resolve the open questions below.
2. `PrecisConfig.owner` + `PRECIS_OWNER` (config.py + README env table).
3. `ask.py`: `ASKER = cfg.owner`; stamp `user:<owner>` on the parked
   todo.
4. Remove the `asking-reto` alias (all §2b sites) + CHANGELOG note.
5. Route `view='attention'` / Asks page / chatter preamble by
   `user:<me>`.
6. Move question prose from the `ask-user:` value into a chunk;
   update `routes/asks.py` reader.
7. (Optional) closed-vocab `user:` axis (§4 Option A).
8. Data migration for `user:` reconciliation if the canonical handle
   ≠ `elmsfeuer`.
9. Docs: note `web:owner` (not `web:reto`) in the two stale design
   docs; refresh `precis-todo-tree-help`.
10. Tests: ask-routing filter, alias-removal regression, config default.

### 7. Definition of done

Standard per AGENTS.md (ruff / mypy / pytest, CHANGELOG, version bump).
Plus: a fresh install with no `PRECIS_OWNER` works (`owner` default);
no `\breto\b` left in `src/` except author attribution; `view='asking-reto'`
gone with a CHANGELOG deprecation line.

### 8. Open questions for Reto

1. **Canonical handle.** ✅ **DECIDED (2026-06-19): `elmsfeuer`** —
   matches the 44 live `user:elmsfeuer` rows, so **no data migration**.
   `PRECIS_OWNER` defaults to `owner` on a fresh install; Reto's
   instance sets `PRECIS_OWNER=elmsfeuer`. The bare `reto` tag (1 row)
   normalizes to `user:elmsfeuer`.
2. **`ANSWERER`/bot identity** — leave `asa` hard-coded (it is stable
   and consistent), or move to config for symmetry?
3. **Question prose relocation** — do it in this change (cleaner, more
   work) or defer and keep the prose-in-tag-value smell for now?
4. **Registry** — open `user:` tag (status quo) vs the closed-vocab
   axis (§4 Option A) — now or later?
5. **`project:reto-*` slugs** — leave as project names, or rename for
   consistency?

## Multiuser access — tailscale-authenticated users, admin/user roles, per-doc visibility

_Grouped 2026-09-26; was `multiuser-access`, status draft, prio normal, blocked-by user-identity-and-ask-routing._

Compiled from the 2026-08-18 design discussion (Reto + agent). Friendly-
academic threat model now (accidental leakage, not malice); designed so the
hardening step (RLS) is a transcription, not a redesign.

### Motivation / why

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

### Design

#### Identity & authentication (Tailscale does this part)

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

#### Data model

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

#### Enforcement (code now, DB backstop later)

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

#### Principals — root workers vs user-originated agents

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

### In scope

- `users` / `shares` tables + `refs.owner_id` + backfill migration.
- `KindSpec.visibility` flag + initial per-kind values.
- `build_visibility_filter` + injection at user-reachable read sites;
  write-authorization check at verb dispatch.
- Viewer-context plumbing: `PRECIS_USER` for SSH/stdio/CLI; tailscale-serve
  header middleware for `precis_web` (closes `factory-post-auth.md`).
- Principal model incl. per-job on-behalf-of in the worker claim loop.
- Derived-artifact owner inheritance in synthesis passes.

### Explicitly NOT in scope

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

### Acceptance criteria

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

### Target + blast radius

- Schema: new tables `users`, `shares`; `refs.owner_id` (+ backfill).
- `src/precis/protocol.py` (`KindSpec`), `store/_tag_filter.py` sibling
  module, `store/_blocks_ops.py`, `store/_refs_ops.py`,
  `tools/core.py::_dispatch`, `handlers/_todo_guards.py`,
  `runtime/factory.py` (viewer context), `store/pool.py` (per-tx GUC).
- `precis_web/app.py` (+middleware), `precis/cli/web.py`, worker claim
  loop (`workers/`), synthesis passes (owner inheritance).
- Deploy: tailscale-serve config for web; per-human unix accounts +
  tailnet SSH ACLs (out-of-tree ansible).

### Open questions / decisions log

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
