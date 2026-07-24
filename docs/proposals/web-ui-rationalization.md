---
status: built
title: Web-UI rationalization — one unified Drive, a Gripes workbench, and a merged System surface
---

# Web-UI rationalization

Umbrella spec for the melchior web-UI cleanup. Subsumes and finishes
`docs/proposals/unified-item-view.md` (the Drive part) and adds two
sibling workstreams the same session decided to do together. Written
from three recon passes over `src/precis_web/` (2026-07-24).

## Motivation

The nav grew organically into ~11 top-level entries + 2 dropdowns, and
the *content* surfaces underneath are worse than the nav: **six bespoke
list pages** (`/items`, `/drive`, `/papers`, `/papers/triage`,
`/refs` consolidated + per-kind, `/papers-needed`, `/tags/refs`) that
each implement *some* slice of "list refs → filter → tag → open" with
no shared query model or row renderer. Separately, **three ops
dashboards** (`/status`, `/factory`, `/budget`) render overlapping
host/spend/worker data — `/budget` literally `import`s `_budget_tote`
from `/status`. And there is **no place to work a gripe** — the dev bug
tracker is only reachable read-only via `/refs` and the Console REPL.

The infrastructure to fix #1 already half-exists: `ItemPresenter`
(`item_view.py`) is a duck-type row contract over every kind, and
`search(kind='*', exclude=[…])` is the MCP twin. We finish that, collapse
the scattered lists into it, merge the three ops dashboards, and build the
gripe workbench.

## Target information architecture

| Zone | Entries |
|---|---|
| **Daily** (always visible) | **Drive** (unified seek+manage) · Tags · ToDo |
| **Attention** (badged, right) | Needs you · Gripes 🆕 · Alerts |
| **Ops ▾** | **System** (Status+Factory+Budget) · Agent Logs · Console · Env · Secrets |

Everything else — Drafts, Papers, Refs, Oracle, Patents, CFPs,
Structures, CAD, Figures, Mermaid, Papers-Needed — loses its **list
tab** and becomes **rows in Drive + its existing detail reader as
click-through**. One list, many readers. ToDo stays its own tree
(bespoke, not a document-kind). Tags stays a top-level diagnostic.

---

## Workstream 1 — Unified Drive (base = Items, graft Drive's folder+CRUD)

**Decision (user):** Items' search/facet/presenter architecture is the
base; graft Drive's folder tree + CRUD onto it; the result is named
**Drive** and served at `/drive`. Items' route/tab retires into it.

### What Items already gives us (keep)
`routes/items.py` + `templates/items/index.html.j2` + `item_view.py`:
cross-kind chunk search (`q=`), kind facets (Source + Author/`role=artifact`),
tag-filter chips w/ autocomplete (`/items/tags/suggest`), `sort=relevance|recency`,
`since/until` date window, `state=stub`, **folder facet** (`folder=` →
`store.list_folders()`), `page=` pagination, kind-selection cookie, and the
per-row flag buttons + hover peek + click-through via `item_row()`.

### What to graft from Drive (`routes/drive.py`)
- **Folder tree sidebar** (`_flatten_tree`, `_children`, `_unfiled`) rendered
  alongside the item list — the persistent left rail.
- **Folder CRUD write routes**, kept at their current paths so existing
  callers (`datasheets.py`, `pres.py`, `cad/figure/mermaid` "+New" posts,
  root `/` redirect) keep working: `POST /drive/new`, `/drive/create`,
  `/drive/{id}/rename`, `/drive/move`, `/drive/{id}/delete`. All already
  dispatch verbs via `redirect_or_error` — no data-layer change.
- **Per-row quick actions** (`ItemPresenter.actions()` seam already exists,
  currently returns `[]`): move-to-folder, delete/unfile, tag — surfaced on
  each row so the unified list is *manage*, not just *seek*.
- **Deleted-ref visibility** and **watch-dir drop-zone info** (from
  `papers_needed.py::_watch_dir_from_plist`) — the two gaps `items.py:15-19`
  names as blocking retirement.

### List tabs that fold in (readers stay as click-through)
- **Papers list** (`/papers`, `/papers/triage`) → Drive rows filtered to
  `kind=paper`; `state=stub`/`has_pdf`/`has_chunks` become Drive facets;
  the **triage queue** becomes a Drive preset (`tag=needs-triage`) — but see
  Risk R5, it's cross-linked from Needs-you and must stay reachable.
  Reader routes `/papers/{ident}` + sidebar/PDF/edit all **stay**.
- **Refs consolidated** (`/refs?all=1`) → Drive is the new "search
  everything" surface; **repoint the loupe** (Risk R1).
- **Refs per-kind lists** incl. **Oracle** (`/refs/oracle`) + **Patents**
  (`/refs/patent`) + **CFPs** → Drive kind-facet presets. **Decision R4
  below.** `refs/detail.html.j2` + `conv_detail.html.j2` readers **stay**.
- **Papers-Needed** (`/papers-needed`) → Drive `state=stub` facet; keep a
  badged "needs fetch" affordance if desired (open decision D3).

### Blast-radius checklist (must all be handled)
- **R1 — the loupe.** `base.html.j2:110` `action="/refs"` + hidden `all=1`
  → repoint to `/drive`. Also `console.py:346,350,683` build `/refs?…all=1`.
- **R2 — flag bounce-back.** `flags.py:138,156` default `return_to=/papers-needed`
  → change default to `/drive`.
- **R3 — `/tags/refs` survives.** `status.html.j2:18,347,392` pivot into it;
  `tags.py:240` flips `active_tab` to `status`. Route stays (feeds System too).
- **R4 (decision) — Oracle/Patents/CFPs.** They nav to `/refs/{kind}` *list*
  pages. Plan: turn each into a Drive preset URL (`/drive?k=oracle` etc.) and
  drop the standalone list route; keep the *detail* readers. Oracle's
  "roll-the-dice" and Patents' OPS remote-search are special UX — if either
  doesn't reduce to a facet cleanly, keep it as a thin standalone reachable
  from a Drive row action (do NOT block WS1 on it).
- **R5 — triage ↔ Needs-you.** `needs_you/index.html.j2:84,102` link
  `/papers/triage`; keep that path working (redirect to the Drive triage
  preset, or keep a thin triage route rendering into Drive).
- **R6 — self-referential pagers/links** in each retiring template + the
  `return_to=/papers` / `/papers/triage` form defaults (`papers.py:993,1159,1302`).
- **Tests:** ~25 `test_items_*`, papers list/triage, `test_papers_needed_*`,
  `test_refs_by_tag_*`, `test_flag_toggle_*`, `test_root_redirects_to_drive`
  (`tests/precis_web/test_routes.py`). Behaviors move, not vanish — port the
  assertions onto `/drive`.

---

## Workstream 2 — Gripes workbench (`/gripes`, new)

**Gap:** no write surface for the dev bug tracker. **Zero data-layer
work** — wire existing verbs, exactly as `/flags` and `/alerts` do.

### Model facts (from `handlers/gripe.py` + `precis-gripe-help.md`)
- STATUS axis is **closed** (replace-on-add): change status with a single
  `tag(kind='gripe', id=N, add=['STATUS:x'])` — no explicit remove.
- Vocabulary: `open → triaged → ready_for_fix → in_review → wontfix`.
- **"Close" is two distinct actions**, both must be exposed and labelled:
  - `STATUS:wontfix` — kept on record, won't act (a tag change).
  - `delete` — retire/resolve (soft-delete; the convention after a fix
    merges). **There is no `done`/`fixed` status.**
- Annotate = `put(kind='gripe', id=N, text=…)` appends an append-only
  `gripe_comment` chunk. Body (`gripe_body`, pos 0) is immutable by design
  (audit trail) — this is the answer to "is read-only sensible?": *the body
  is, the surface shouldn't be.*

### Build
- `routes/gripes.py`:
  - `GET /gripes` — list live gripes (default: all non-`wontfix`; toggle to
    `wontfix`/all). SQL mirrors `alerts.py::_rows` but on `t.namespace='STATUS'`;
    order by a STATUS CASE-rank then `updated_at`. Group by STATUS.
  - `GET /gripes/{id}` — detail: body + comment timeline via
    `store.list_blocks_for_ref` (walk `chunk_kind` body/comment), render
    markdown w/ `linkify_refs`; a comment box + status controls.
  - `POST /gripes/{id}/status` — `tag` verb, closed-axis add. htmx-fragment
    (re-render status badge) + no-JS redirect split, per `flags.py`.
  - `POST /gripes/{id}/comment` — `put` verb append.
  - `POST /gripes/{id}/retire` — `delete` verb (distinct from wontfix; confirm).
  - Optional `POST /gripes/{id}/fix` — mint a `fix_gripe` job (nice-to-have).
- `templates/gripes/list.html.j2` (model on `alerts/list.html.j2`) +
  `gripes/detail.html.j2` + a `_gripe_status.html.j2` fragment.
- **Nav badge** `nav_gripes`: add `_gripes_count(store)` to `nav.py`
  (count `STATUS`-live, matching the default list filter), wire into
  `nav_badges` + the stateless early-return, add the badged `<a href="/gripes">`
  next to Alerts in `base.html.j2` (distinct colour — e.g. `bg-violet-500`).
- Register `gripes.router` in `app.py`.

---

## Workstream 3 — System surface (merge Status+Factory+Budget)

**Base = `/status`** (only read-only one; already owns `_budget_tote`,
the host strip, and a first-class nav slot). Sub-tabs:

- **Health** (default) — status Machines + Liveness + Background health +
  factory Hosts strip/capability chips. Reconcile the duplicated host strip
  (status reads `worker_logs`+`host_heartbeat` over 24h; factory reads
  `host_heartbeat`+`worker_logs` over 6h) into one; keep the lazy
  `GET /status/backlog` fragment.
- **Services** — factory category tables + **editable prio + model_pref**
  (writes → `service_config`) + Quests char-budget shares. Keep
  `POST /factory/{prio,model,clear}` working (re-anchor redirects to the
  Services sub-tab).
- **Budget** — the tote + `quota.evaluate` live pause banner + cap
  set/reset + resume-override (writes → `app_settings`). Fold
  `budget/index.html.j2` in; keep `POST /budget/{set,reset,resume,resume/clear}`.

### Blast radius
- Nav: drop `('factory',…)` + `('budget',…)` from the `ops` tuple
  (`base.html.j2:78`); repoint the `/status` top-level entry to the new
  System tab (or rename label to "System").
- `status.html.j2:257` `edit caps →` link → Budget sub-tab anchor.
- `budget.py:21` `from …status import _budget_tote` — keep the function
  importable (or lift into a shared `_system/` module) when reorganizing.
- `tags.py:240` sets `active_tab="status"` for `/tags/refs` pivots — keep
  the System tab highlighting for those.
- **Tests hardcode paths:** `test_budget_route.py` (asserts `/budget` +
  `action=` + redirect `location`s), `test_factory.py` (`/factory`,
  `?host=`, POST redirects), `test_routes.py` status assertions. Update to
  the new URLs/anchors.

---

## Sequencing (coder rounds — each a decided slice, own acceptance check)

Ordered so each slice ships green independently; nav restructure last so
tabs never point at a half-built surface.

1. ✅ **WS2 Gripes** — self-contained, no retirement risk. New route+templates+badge.
   *Accept:* `/gripes` lists, status-change/comment/retire work, badge counts.
2. ✅ **WS1a Drive graft** — add folder tree + CRUD + per-row actions to the Items
   surface, serve it at `/drive` (Items tab → Drive). Keep old list tabs alive.
   *Accept:* `/drive` does everything Items+Drive did; `test_items_*` ported.
3. ✅ **WS1b Retire + repoint** — fold papers/refs/papers-needed lists in; repoint
   loupe (R1), flag default (R2), triage (R5); Oracle/Patents/CFPs presets (R4).
   *Accept:* loupe → Drive; retired routes gone or redirecting; readers intact;
   `/tags/refs` still serves Status.
4. ✅ **WS3 System** — merge Status/Factory/Budget under sub-tabs; port writes+tests.
   *Accept:* one System page, Services/Budget writes work, factory+budget tests updated.
5. ✅ **Nav restructure** — final `base.html.j2` IA (table above) + docs
   (`state-map.md`, retire `unified-item-view.md` via docs-triage, update skills
   if any reference the old tabs). Includes the leftover Drafts-list retirement
   (`/drafts` → `/drive?k=draft&submitted=1`, mirroring WS1b's papers pattern) —
   it fell between WS1a/WS1b scopes and shipped alongside this slice.

Delegate each slice to a fresh `coder` (sonnet) with the relevant section of
this file as its spec; verify with `scripts/test --impacted` per slice.

## Open decisions (resolve before or during the slice that needs them)
- **D1 — surviving name** confirmed **Drive**. (settled)
- **D2 — Oracle/Patents/CFPs** as Drive facets vs thin standalone readers (R4).
  Lean: facets; keep Oracle dice / Patents OPS-search as row-actions if they
  don't reduce cleanly.
- **D3 — Papers-Needed** fully folded (`state=stub` facet) vs a retained badged
  "needs fetch" attention entry. Lean: fold, no badge (fetcher works it auto —
  the original `nav.py` rationale for excluding it from Needs-you).
- **D4 — ToDo** revamp/pagination — explicitly out of scope this pass.
