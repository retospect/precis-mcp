# Precis-Web Plan — first web interface for the cluster

Status: **queued** — plan captured; first slice is the todo-tree editor.

A FastAPI service that imports the `precis` Python package directly,
renders server-side via Jinja, sprinkles HTMX for interactivity, and
exposes editing surfaces for Reto over Tailscale LAN. **First slice:
the todo-tree editor** (companion to `todo-tree-plan.md`). Future
slices add memory browser, paper viewer, dream feed, and any other
kind that benefits from a visual surface.

The point of this plan is not the tree editor specifically — it's
to **set up the pattern** for cluster web surfaces. There will be
more.

## Design principles

1. **Work off the db.** No caches the user has to invalidate; no
   eventual consistency. Every page render hits postgres; every
   write commits immediately. The DB is the source of truth, the
   web is a viewer.
2. **Share the precis handler layer.** Web writes go through the
   same `TodoHandler.put` / `MemoryHandler.put` etc. that MCP
   writes go through. Same depth check, same level-gradient guard,
   same cycle check, same first-line discipline. Zero risk of
   surface drift.
3. **Server-rendered first.** HTML + HTMX over an SPA. The server
   owns rendering; the client owns local interaction state
   (collapse, modals, drag). One round-trip per edit, always live.
4. **One pattern, many surfaces.** Route module per kind. Adding a
   memory browser later means `routes/memories.py` + `templates/
   memories/`, not a new architecture.
5. **Reto-only for now.** Single bearer token via Tailscale.
   Multi-user / federation comes if/when another user shows up.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Web framework | **FastAPI** | Async-friendly, type-checked, plays nicely with `precis`'s sync/async store |
| Templates | **Jinja2** | Already a Python dep elsewhere; server-rendered fragments for HTMX |
| Interactivity | **HTMX** (~14kb, no build) | DOM-targeted swaps; no SPA toolchain; supports out-of-band updates if we want live feed later |
| Client-side state | **Alpine.js** (~15kb, CDN) | Tree expand/collapse, modals, no bundler |
| Styling | **Tailwind via CDN** initially | No build pipeline until enough scale to want one |
| Auth | **Bearer token via Tailscale** | Single user; cookie session if we add a login flow later |
| Process | **uvicorn**, single worker | Single user, no concurrency story needed |
| Hosting | **caspar daemon** | Co-located with postgres; LAN-only via Tailscale |

No React, no SPA, no Node toolchain. If we want richer interactivity
later (a graph viewer, a drag canvas, a Monaco-edited paper), we
can adopt heavier tools per-page — but the default is server-
rendered.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Browser (Reto, on Tailscale)                            │
│  HTML pages + HTMX swaps + Alpine state                  │
└────────────────────┬─────────────────────────────────────┘
                     │  HTTP/JSON or HTMX fragments
                     │
┌────────────────────▼─────────────────────────────────────┐
│  precis_web (FastAPI on caspar)                          │
│  - routes/<kind>.py per editable kind                    │
│  - templates/<kind>/*.html.j2                            │
│  - middleware: auth, error handling, request logging     │
│  - imports `precis` package; uses Store + handlers       │
└────────────────────┬─────────────────────────────────────┘
                     │  Python function call (no protocol)
                     │
┌────────────────────▼─────────────────────────────────────┐
│  precis package (Store, handlers)                        │
│  - same code path MCP server uses                        │
│  - validation, depth check, level guard, cycle check     │
└────────────────────┬─────────────────────────────────────┘
                     │  psycopg
                     │
┌────────────────────▼─────────────────────────────────────┐
│  Postgres (caspar)                                       │
└──────────────────────────────────────────────────────────┘
```

Web backend lives **as a sibling package** to `precis/`:
`precis-mcp/src/precis_web/` (same monorepo, optional install
extra `precis-mcp[web]`). It imports `precis.store.Store` and the
handler classes directly. No HTTP-to-MCP translation, no protocol
gymnastics.

### Why import precis directly instead of going through MCP

MCP is optimized for **LLM tool calls** — small JSON-RPC envelopes,
batched function-call patterns. For a UI you want:

- Rendering helpers that take handler results and emit HTML
- Form-shaped inputs (not free-form text)
- Per-page request flows (not turn-shaped)
- Direct database transactions on form submit (not RPC)

Going through MCP would add a translation layer with nothing in
return — and since both surfaces ultimately call `Store.put_todo`
etc., direct import keeps validation single-sourced without the
network hop.

### Identity at the handler layer

Web requests carry `source='web:reto'` into the handler. The
level-gradient guard (todo-tree-plan, knob 1) recognizes any
`web:*` source as owner and allows strategic/tactical edits. The
MCP guard recognizes worker sources (`asa-chatter`, `asa-worker`)
and rejects strategic/tactical mutations. Same handler, same row,
different identity → different authorization.

## Slice 1 — Todo-tree editor

The first surface and the only one in this plan's scope. Future
surfaces get their own follow-up slice notes.

### Routes

| Route | Method | Action |
|---|---|---|
| `/tasks` | GET | Strategic dashboard (top-2 levels + today's accounting) |
| `/tasks/{id}` | GET | Detail page for a node (with subtree fragment) |
| `/tasks/{id}/subtree` | GET | HTMX fragment: subtree rendered (for expand) |
| `/tasks/{id}/detail` | GET | HTMX fragment: detail panel for selected node |
| `/tasks` | POST | Create a new strategic root |
| `/tasks/{parent_id}/children` | POST | Create child under parent |
| `/tasks/{id}` | PUT | Edit text, outcome, tags, status, target, priority |
| `/tasks/{id}/parent` | PUT | Move (re-parent) |
| `/tasks/{id}` | DELETE | Delete (cascades to children with confirm) |
| `/tasks/doable` | GET | Doable queue page |

All write endpoints are idempotent against the same form data and
return either a redirect to the GET page or an HTMX fragment.

### Page shape

Two-pane layout:

```
┌─────────────────────────┬────────────────────────────────────┐
│ Tree (collapsible)      │ Detail of selected node            │
│                         │                                    │
│ #42 Nanocube  [3/9 ◄]   │ #67 Write the boxel paper          │
│  ├ #67 Boxel paper      │  level: tactical                   │
│  │   ├ #98 Methods      │  status: open                      │
│  │   │   ├ #114 ◀ asa   │  7d: 9 picks · 72m                 │
│  │   │   ├ #115 ⏸ wait  │                                    │
│  │   │   └ #116 ○       │  outcome:                          │
│  │   ├ #99 Results      │  ┌─────────────────────────────┐   │
│  │   └ #100 Discussion  │  │ Submitted to JCP, all figs  │   │
│  └ #88 Hygiene          │  │ camera-ready                │   │
│ #56 Personal            │  └─────────────────────────────┘   │
│                         │                                    │
│ [+ strategic]           │  tags: …                           │
│                         │  parent: #42 [Change…]             │
│ Header: Today: 5/12     │  cost: M                           │
│ done · 4h elapsed       │                                    │
│                         │  [Save] [Delete] [Move…]           │
└─────────────────────────┴────────────────────────────────────┘
```

### Operations

| Op | UI | Backend |
|---|---|---|
| Edit text/outcome/tags | Inline editor in detail panel; `PUT /tasks/{id}` on save | `TodoHandler.edit` |
| Add child | "+ Add child" button in tree; quick-input modal | `TodoHandler.put` with `parent_id` |
| Add strategic root | "+ Strategic" button at top of tree | `TodoHandler.put` with `parent_id=null` + `level:strategic` tag |
| Delete | Trash icon → confirm dialog showing descendant count | `TodoHandler.delete` cascades |
| Move parent | "Move…" button → tree picker (excludes self + descendants); confirm if crosses level boundary | `TodoHandler.edit` updates `parent_id` |
| Status change | Dropdown in detail panel | `TodoHandler.edit` |
| Set target | Number input on strategic detail (only) | tag rewrite via `TodoHandler.tag` |
| Pause / resume | Toggle in detail panel | `TodoHandler.edit` status |
| Toggle expand | Alpine.js (client-side); HTMX lazy-loads subtree fragments | `/tasks/{id}/subtree` returns rendered HTML |

Live accounting in header polls `/tasks/accounting/today` every 30s
(HTMX trigger) — shows `today: 5/12 done · 4h elapsed`. Pure
SQL via the recursive CTE in the tree plan; cheap.

### What this slice does NOT do

- **No drag-and-drop reparenting in slice 1.** Move-via-picker
  only. DnD is fiddly to get right; add later if it earns its
  keep.
- **No graph view.** Tree only. The tree is the data shape; a graph
  view of cross-strategic links comes later when the links table
  is more populated.
- **No mobile-optimized layout.** Desktop-first. PWA shell later.
- **No realtime collaboration.** Single user. Polling for live
  accounting is fine.
- **No undo/redo.** Postgres has the row history if we want it
  later; the UI doesn't expose it yet.

## File layout

New `precis-mcp/src/precis_web/`:

```
src/precis_web/
  __init__.py
  app.py                       — FastAPI app factory
  config.py                    — env-driven config (DB url, bind addr, auth token)
  auth.py                      — bearer-token middleware
  identity.py                  — source='web:reto' injection helper
  errors.py                    — exception → HTML/HTMX-fragment mapper
  routes/
    __init__.py
    tasks.py                   — all /tasks/* endpoints
    accounting.py              — live header poll endpoint
  templates/
    base.html.j2               — page chrome, CSS includes, header
    tasks/
      dashboard.html.j2        — full /tasks page
      _tree_node.html.j2       — recursive partial for the tree side
      _detail.html.j2          — right pane partial
      _form.html.j2            — add/edit form fragment
      _move_picker.html.j2     — parent picker modal
  static/
    htmx.min.js                — vendored
    alpine.min.js              — vendored
    tailwind.config.js         — CDN config initially
    app.css                    — minimal custom + Tailwind utilities
tests/precis_web/
  test_auth.py
  test_tasks_routes.py         — every route, every operation
  test_identity_guard.py       — verifies web:reto can edit strategic, asa-worker can't
  test_render_safety.py        — XSS, escape correctness, big-tree timing
```

Changed:

- `pyproject.toml` — optional `[web]` extra:
  `fastapi`, `jinja2`, `uvicorn[standard]`, `python-multipart`.
- `precis-mcp/README.md` — point at this plan for the web surface.
- `src/precis/data/skills/precis-overview.md` — mention the
  precis-web URL pattern so asa-bot can suggest it in chat.

In `cluster/`:

- `cluster/roles/precis_web/` — new Ansible role
  - `tasks/main.yml` — install extra, deploy LaunchDaemon plist,
    set up token in `/etc/precis-web/env`
  - `templates/com.precis.web.plist.j2` — daemon spec (uvicorn,
    bind to 127.0.0.1:9100, env file)
  - `handlers/main.yml` — bootout/load/kickstart pattern
- `cluster/playbooks/34-precis-web.yml` — site entry
- `cluster/site.yml` — append "34 — precis web"
- `cluster/inventory/group_vars/all/precis_web.yml` (encrypted) —
  bearer token

## Deployment

- Service binds **`127.0.0.1:9100`** — Tailscale handles the LAN
  exposure; localhost-only on the box.
- Bearer token in env, sourced from
  `/etc/precis-web/env` (mode 0600, owned by precis user).
- Reto's browser hits `http://caspar:9100/` over Tailscale.
- Caddy reverse proxy + Tailscale magicdns for HTTPS later if we
  want it; not required for slice 1.

## Open decisions

1. **Repo location** — sibling package in `precis-mcp` (recommended;
   single repo, single PR for tightly-coupled edits) vs separate
   `precis-web` repo (cleaner separation, more CI overhead).
   Recommendation: sibling package, optional install extra.

2. **Token rotation** — manual via Ansible playbook (set new token,
   rerun) vs auto-rotated. Recommendation: manual for now; single
   user, rotation is rare.

3. **Tree picker for Move** — full-tree modal vs typeahead by id
   or title. Recommendation: both — typeahead with collapsible
   tree fallback. Reto knows the ids of his strategics; typeahead
   is faster.

4. **Default landing page** — `/tasks` (strategic dashboard) vs
   `/tasks/doable` (what to work on now). Recommendation:
   `/tasks` — Reto opens this to plan, not to grind a queue.

5. **Real-time updates from worker activity** — should the page
   reflect a worker marking a leaf done while Reto is looking at
   it? Polling (every 30s for accounting; full-page polling for
   tree changes) vs SSE push. Recommendation: polling for slice 1;
   SSE if the latency becomes annoying.

## Phasing

Single slice for the tree editor. Future slices in this plan
(separate sections to be added):

- **Slice 2 — Memory browser.** `/memories` list + detail; tag
  filter; recent / pinned / decay-soon views; inline edit.
- **Slice 3 — Paper viewer.** `/papers/{slug}` with chunked TOC,
  abstract, linked-in/out, "open in editor" button.
- **Slice 4 — Dream feed.** `/dreams` chronological view of
  speculative connections, "promote to memory" / "let decay"
  affordances.
- **Slice 5 — Cron + job dashboard.** What's scheduled, what's
  running, what failed last.

Each future slice adds a `routes/<kind>.py` + `templates/<kind>/`
under the established pattern.

## Estimated work

- Skeleton (FastAPI app + Jinja base + auth + identity wiring + one
  hello route): ~0.5 session.
- Tree editor (all routes, templates, forms, move picker, live
  accounting header): ~1 session.
- Polish + tests + Ansible role + deploy: ~0.5 session.

~2 sessions for slice 1 end-to-end. Smaller than expected because
we reuse `precis`'s handler validation rather than reimplementing it.

## Not in scope (this plan)

- **Generic admin panel for arbitrary kinds.** Each kind is its
  own surface — they have different shapes, different operations.
  A generic CRUD admin would be uglier than per-kind surfaces.
- **Mobile app / native iOS-Android.** PWA shell of this same web
  surface is a future option; native apps are not on the roadmap.
- **Multi-user.** Single bearer token; one user. Federation
  later if relevant.
- **Real-time multi-client sync.** No collaborative editing.
- **Theming / settings.** Hardcoded styling for now.

## Relationship to other plans

| Plan | How this plan composes with it |
|---|---|
| `todo-tree-plan.md` | This plan's Slice 1 is the human-facing editor for that plan's tree. Tree plan ships first (the substrate); this can follow once handlers are stable. |
| `goal-kind-plan.md` | Future slice will add a goal-charter editor (file-backed markdown editing); reuses the same FastAPI + Jinja pattern. |
| Existing `asa_bot` Discord daemon | Orthogonal — Discord is chat, web is structured editing. Both call into precis. |
| Existing precis-mcp MCP server | Sibling, not replaced. MCP serves LLM clients; web serves Reto. Both share handler layer. |
