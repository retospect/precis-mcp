# Precis-Web Build — concrete build slice (4 tabs, no-auth)

Status: **in progress** — supersedes the scope of `precis-web-plan.md`
Slice 1 with a broader first cut requested by Reto (2026-06-13).

This is the **build doc** for the first shipped `precis_web` service.
It records the decisions that diverge from / extend the two queued
plans it builds on:

- `precis-web-plan.md` — the FastAPI + Jinja + HTMX pattern, file
  layout, handler-reuse principle. Still authoritative for the
  architecture. This doc widens its Slice 1 (tree-editor-only) to a
  four-tab first cut and removes auth.
- `todo-tree-plan.md` — the hierarchical `kind='todo'` substrate. The
  tasks tab needs it, so this slice implements the tree handler too.

## What Reto asked for

> precis needs a webserver; it has many aspects. One is management of
> the todo hierarchy. Another is reading papers in a browser (PDFs on
> the NFS server, port-forwarded from a cluster node). Also a tab that
> shows status. Show the whole thing without authentication for now. I
> also want a way to call the tools interactively (a precis-query
> thing), and a status page (papers ingested, etc).

Four tabs, no auth, LAN-only over Tailscale.

## Tabs

| Tab | Route prefix | Backed by |
|---|---|---|
| **Tasks** | `/tasks` | new todo-tree handler (this slice) via in-process dispatch |
| **Papers** | `/papers` | paper kind search + PDF served from `corpus_dir` |
| **Console** | `/console` | in-process `PrecisRuntime.dispatch` (the seven verbs) |
| **Status** | `/status` | direct SQL summaries (corpus / ingest / worker health) |

Landing page redirects to `/tasks`.

## Divergences from the queued plans

### D1 — No auth (this slice)

`precis-web-plan.md` specifies a bearer token. Reto explicitly wants
none for now ("show the whole thing without authentication"). The
service binds to `127.0.0.1:<port>` and is reached over Tailscale; the
network boundary is the only control. `config.py` keeps an *optional*
`auth_token` field (unset = no auth) so a token can be switched on
later without a code change. No login flow.

### D2 — Four tabs, not tree-only

The plan's Slice 1 was the tree editor alone. We ship all four tabs in
the first cut because three of them (papers, console, status) need
zero new substrate — they read existing handlers / tables. Only the
tasks tab needs the tree handler, which we build here.

### D3 — Actor identity uses the existing `PRECIS_SOURCE` env (ADR 0013)

The level-gradient guard in `todo-tree-plan.md` (knob 1) needs to know
*who is calling* (owner vs worker). That plumbing already exists:
`src/precis/handlers/_todo_guards.py` reads `PRECIS_SOURCE` (the ADR
0013 session-context env var) at guard time and classifies:

- unset / `cli` / `user` / `web:*` → **owner** (may write
  `level:strategic` / `level:tactical`).
- `asa-*` (chatter / worker / dreamer) → **worker** (rejected on
  strategic/tactical create, tag, and destructive ops).
- anything else → **owner** (forward-compatible; a typo'd source
  leaves the guard inert rather than silently demoting a worker).

The web app's process therefore sets `PRECIS_SOURCE=web:reto`; worker
daemons set `PRECIS_SOURCE=asa-worker`. No new config field, no
`Hub.actor` — we reuse the established mechanism. (`set_by` provenance
on the underlying writes stays `"agent"` for this slice; promoting it
to carry the real source is a separate, deferred enhancement.)

### D4 — `corpus_dir` config — new

PDF files live at `<corpus_dir>/<letter>/<cite_key>.pdf` (the
`precis watch` layout — `letter` = lowercase first alnum char of the
cite_key, else `_`). We add `PrecisConfig.corpus_dir` (env
`PRECIS_CORPUS_DIR`, default `~/work/corpus`). On the cluster this is
the NFS mount; the web node port-forwards nothing itself — uvicorn
binds loopback and Tailscale exposes it. The viewer streams the PDF
through the app (`/papers/{id}/pdf`) so the browser never needs direct
NFS access.

## Tree handler (todo-tree substrate) — already built

The todo-tree substrate this tab needs is **already implemented in the
tree** (landed in parallel with this web work):

- `src/precis/handlers/todo.py` — `parent_id` on `put`, level guard,
  ancestry on `get`, and the `view=` router.
- `src/precis/handlers/_todo_views.py` — renderers for `roots`,
  `strategic`, `doable`, `waiting`, `blocked`, `asking-reto`, `tree`.
- `src/precis/handlers/_todo_guards.py` — parent-exists, cycle, depth-10,
  and the `level:strategic|tactical` owner-only gradient (reads
  `PRECIS_SOURCE`).
- `src/precis/workers/auto_check_evaluators.py` — auto-check worker.
- `tests/test_todo_tree.py` — unit coverage.

This web slice therefore **consumes** that handler through the
in-process runtime (`runtime.dispatch('search', {'kind':'todo',
'view':'doable'})`, `dispatch('put', {'kind':'todo','parent_id':N,…})`,
`dispatch('tag', …)`, `dispatch('delete', …)`). It adds **no** tree
SQL of its own — the no-surface-drift principle from `precis-web-plan.md`
holds: the web and MCP write the tree through the identical handler.

Re-parenting (the "Move…" op) is the one tree mutation not yet exposed
as a verb. For cut 1 the tasks tab omits drag/move (matching the
plan's "no DnD in slice 1"); a `reparent` affordance lands once the
handler grows a verb for it.

## Stack

Per `precis-web-plan.md`: **FastAPI + Jinja2 + HTMX + Alpine + Tailwind
(CDN)**, server-rendered, single uvicorn worker. No React/SPA/Node
toolchain. HTMX/Alpine/Tailwind pulled from CDN in `base.html.j2` for
the first cut (vendoring is a later polish step). PDF rendering uses
the browser's native `<iframe>`/`<embed>` PDF viewer — no pdf.js
bundle in cut 1.

## File layout

```
src/precis_web/
  __init__.py
  app.py            — FastAPI app factory (mounts routers, static, templates)
  config.py         — env-driven config (DB url, bind, corpus_dir, actor, optional token)
  deps.py           — shared runtime/store singletons + Jinja env
  errors.py         — PrecisError -> HTML/fragment mapper
  routes/
    __init__.py
    tasks.py        — /tasks/* (tree editor)
    papers.py       — /papers/* (list, detail, /pdf stream)
    console.py      — /console (interactive dispatch)
    status.py       — /status (+ /status/data poll)
  templates/
    base.html.j2
    tasks/*.html.j2
    papers/*.html.j2
    console.html.j2
    status.html.j2
src/precis/cli/web.py   — `precis web` subcommand (uvicorn launcher)
tests/precis_web/
  test_app.py
  test_tasks_routes.py
  test_papers_routes.py
  test_console_routes.py
  test_status_routes.py
tests/handlers/test_todo_tree.py   — tree handler unit tests
```

## Deployment

- `precis web --host 127.0.0.1 --port 9100` launches uvicorn.
- Optional `[web]` extra: `fastapi`, `jinja2`, `uvicorn[standard]`,
  `python-multipart`.
- Reached over Tailscale at `http://<node>:9100/`. No auth in cut 1.

## Definition of done (this slice)

- Tree handler unit tests green; level guard verified (web:reto can
  write strategic, asa-worker cannot).
- Each route has a happy-path test; PDF stream test uses a tmp corpus.
- Console executes a real verb round-trip in-process.
- `uv run ruff check . && uv run ruff format --check . && uv run mypy
  src tests && uv run pytest` passes.
- `[web]` extra + `precis web` documented in README; CHANGELOG entry;
  version bump; ADR 0026 (precis-web surface) written.
