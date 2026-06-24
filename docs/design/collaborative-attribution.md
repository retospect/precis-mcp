# Collaborative multiuser — attribution, not isolation

- **Status**: proposed (2026-06-23).
- **Authors**: Reto + agent.
- **Builds on**: `user-identity-and-ask-routing.md` (the `user:<owner>`
  axis, `PrecisConfig.owner` / `PRECIS_OWNER`, the `ask-user` →
  `user:<who>` binding). This doc reuses that machinery; read it first.

## 1. The ask, restated

Let several people use precis together. The main surface for these
users is **the web UI, specifically the drafts viewer/editor**. The
work is **collaborative** — everyone can see and edit everything. The
only new requirement is that it be **nice to see who did what**.

That single sentence sets the whole scope: this is **attribution, not
isolation**. There is no per-user data, no access control, no hidden
rows. The corpus, the todo tree, thoughts, drafts — all stay shared
exactly as today. We add *who* to every action and *show* it.

The group is trusted and bounded; "some thought leakage" between
members is fine because there is no leakage to worry about — it is all
one shared workspace, now with names on the changes.

### What this is NOT (explicitly out of scope)

- ❌ Per-user default filtering of drafts / search / todos.
- ❌ Per-user todo trees or planner rotations.
- ❌ Memory / thought isolation. The corpus stays fully shared.
- ❌ An access-control or row-security layer. No Postgres RLS.
- ❌ Threading per-request identity through the MCP / dispatch /
  worker stack. Those stay single-owner-per-process (`PRECIS_OWNER` /
  `PRECIS_SOURCE`), untouched.

If real multi-tenant isolation is ever needed (untrusted users, a
hosted deployment), that is a separate, larger effort — the per-request
auth + identity-threading road this doc deliberately does **not** take.

## 2. Why the web is the only surface that changes

Identity today is **per-process**: `_caller_source()`
(`handlers/_todo_guards.py`) reads `PRECIS_SOURCE` once at boot; the
web sets `PRECIS_OWNER` / `WebConfig.owner` at startup and freezes it.
That is fine for the CLI, the MCP server an agent talks to, and the
workers — each is one actor per process.

The web is different: **several humans share one server through their
browsers**. So the web — and only the web — needs to learn *who this
request is* and stamp that on the writes it makes. `precis_web` is a
sibling package over the handler layer (ADR 0026) and the handlers
already accept `tags=`, so the web can pass identity **down as data**
without any handler-signature surgery. The cost stays inside
`src/precis_web/`.

## 3. Three pieces

### 3a. Per-request identity at the web layer (the one infra piece)

`WebConfig.owner` becomes a **per-request** value instead of a frozen
process default. Source of truth: an identity-injecting reverse proxy
in front of `precis web`.

- **Recommended**: front the web with `tailscale serve` (or
  oauth2-proxy) and read the authenticated user from the forwarded
  identity header. Zero passwords, real identity, matches the trusted
  tailnet group. The existing `PRECIS_WEB_AUTH_TOKEN` becomes the
  proxy↔app shared secret (the app trusts the header only when the
  token matches).
- A `current_user(request)` helper resolves the header → a canonical
  username (falling back to `cfg.owner` when no proxy is configured, so
  a solo/local run is unchanged and zero-config).
- Everything below the web boundary keeps working as-is; the web
  passes the resolved user explicitly into the specific handler/store
  calls it makes.

### 3b. Attribution on writes (no schema change)

Two existing homes carry the human identity. `set_by` stays the coarse
actor *class* (`agent` / `user` / `system` — it is FK'd to the
`actors` table); we do **not** seed per-human actor rows or touch that
FK. The human rides alongside:

1. **Draft chunk edits → `chunk_events.source` (JSONB).** Migration
   0031 already logs every `created` / `edited` / `moved` /
   `reparented` / `retired` event with a `source` provenance envelope.
   We grow the envelope:

   ```json
   {
     "actor": "asa",            // who performed the edit (human or bot)
     "on_behalf_of": "alice",   // the human who triggered it; null for a direct edit
     "change_request": 4821,    // existing — the todo that drove it
     "brief_sha": "…"           // existing
   }
   ```

   Threaded through `_draft_ops.edit_text` / `move_chunk` /
   `retire_chunk`. Direct human edit: `actor="alice"`,
   `on_behalf_of=null`. Agent edit on Alice's behalf: `actor="asa"`,
   `on_behalf_of="alice"`.

2. **Draft / todo / ask *refs* → a `user:<who>` tag.** The
   `default_tags` injection hook (`default_tags.py`) already runs on
   note-like kinds at `put()` time; the web supplies `user:<current>`
   as a default tag so "who created this" is queryable on the existing
   indexed tag axis. (Not `meta.owner` — it is unindexed and null on
   all prod rows.)

### 3c. Render it

- **Drafts grid** (`routes/drafts.py`) already has a per-chunk meta
  column and an in-flight-requests column. Attribution slots in:
  "alice edited 2h ago", "alice (via asa)" when `on_behalf_of` is set,
  "bob requested this change". The per-row version token / live-refresh
  path already exists to keep it fresh.
- **Asks list** (`routes/asks.py`) shows each ask/change-request's
  author. Optional **"asks for me"** filter via the `user:<who>`
  addressee from the sibling doc — but everyone still sees all asks by
  default (collaborative).

### Rendering rule for `source`

Show `on_behalf_of` as the primary name when present, else `actor`;
note the agent secondarily. "alice (via asa)" for an agent edit Alice
triggered; plain "bob" for a direct human edit. The same envelope
covers asks/change-requests: the requester is the human; a
planner-filed request carries them in `on_behalf_of`.

## 4. Data model touchpoints

| Datum | Mechanism | New? |
|-------|-----------|------|
| Who edited a draft chunk | `chunk_events.source.{actor,on_behalf_of}` | envelope field only — column exists (0031) |
| Who created a draft/todo/ask ref | `user:<who>` tag (existing axis) | no — reuses `default_tags` + the tag axis |
| Actor *class* | `set_by` → `actors` FK | unchanged; no per-human rows |
| Ask addressee | `user:<who>` tag | per `user-identity-and-ask-routing.md` |
| Per-request web user | proxy identity header → `current_user()` | new, web-only |

**No migration.** Everything rides existing columns and the existing
tag axis.

## 5. Implementation steps

1. Land this doc; resolve open questions (§7).
2. **Web identity**: `current_user(request)` helper reading the proxy
   header, gated by `PRECIS_WEB_AUTH_TOKEN`; default to `cfg.owner`
   when unconfigured. Wire it through the routes that write.
3. **Draft attribution**: extend the `chunk_events.source` envelope
   with `on_behalf_of`; thread the web user (and triggering agent, if
   any) into `_draft_ops.edit_text` / `move_chunk` / `retire_chunk`.
4. **Ref attribution**: web supplies `user:<current>` as a default tag
   on draft / todo / change-request `put()`s.
5. **Render**: attribution in the drafts grid rows + asks list;
   "alice (via asa)" rule. Optional "asks for me" filter.
6. **Ask author**: `ask.py` `ASKER` resolves to the per-request user,
   not the process `cfg.owner`.
7. Tests: identity-header resolution + token gate; `source` envelope
   round-trip (direct vs on-behalf); default-tag stamping; render
   snapshot.
8. Docs: web deploy note (proxy in front), `precis-overview` /
   drafts-help mention of attribution.

## 6. Definition of done

Standard per `AGENTS.md` (ruff / mypy / pytest, CHANGELOG, version
bump). Plus: a solo/local web run with no proxy and no
`PRECIS_WEB_AUTH_TOKEN` behaves exactly as today (attribution falls
back to `cfg.owner`); a proxied run stamps the real user on draft edits
and new refs; the drafts grid shows who-did-what including the
"via <agent>" case.

## 7. Open questions for Reto

1. **Proxy choice** — `tailscale serve` identity header vs oauth2-proxy
   vs a different front. (Recommendation: tailscale, since the cluster
   already lives on the tailnet.)
2. **Username canonicalisation** — the proxy will hand back some
   identity string (tailnet login, email). What maps it to the
   canonical handle (`elmsfeuer`, etc.)? A small env/config dict, or
   take the proxy string verbatim?
3. **Direct-edit actor** — for a human editing in the web with no agent
   involved, set `source.actor` to the human (`"alice"`) directly, or
   keep `actor="user"` (the class) and put the human only in a
   sibling field? (Leaning: human in `actor`, `on_behalf_of=null`.)
4. **"Asks for me" filter** — build it now alongside attribution, or
   defer until there is a real second user?
5. **CLI/agent attribution** — when an MCP agent (not the web) edits a
   draft, it has no per-request human. Leave `on_behalf_of=null` (just
   the agent), or let `PRECIS_OWNER` fill it? (Leaning: null — the
   web is the surface that knows the human.)
