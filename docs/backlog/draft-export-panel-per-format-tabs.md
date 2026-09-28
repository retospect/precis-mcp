---
status: draft
title: Draft export UI — Check/Export split + per-format tabs
---

# Draft export UI — Check/Export split + per-format tabs

## Motivation / why

The smartdraft export controls (`src/precis_web/templates/smartdraft/view.html.j2`, ~line 1109) render eight links/buttons and three checkboxes in one flat row. The checkboxes do not indicate which formats they affect: `+ sources` applies to PDF and docx; `allow placeholder figures` only to reMarkable; `force` only to the retraction check. Controls are opaque.

Separately, two exporter options exist in code but are unreachable from the browser: `doi_links` and `library_links` (independent booleans, both settable via CLI with `--no-doi-links` / `--no-library-links` and via `draft_export` job params). Web download routes never pass them, so both are hard-wired on. This forces every in-text cite to render as `[203] doi UL` — the two link words cannot be switched off from the UI.

A third issue: the export gate is applied inconsistently across formats — gripe 454753. The `GET /drafts/{ident}/pdf` route, the most-pressed export control, applies neither the retraction gate nor figure clearance, while the docx and job paths apply one or both.

## In scope

Three slices, in order:

1. **Fix the export gate inconsistency** (separate and parallel). Correctness bug, independent of layout, and the tab design assumes checks are draft-level properties.

2. **Thread `doi_links` / `library_links` through web download routes** to the exporters that already honour them. No UI changes yet — just plumb the params through.

3. **Refactor the Tools pane layout**: split the checks out of Export into their own block, and give Export per-format tabs.

The Tools pane (right-rail bottom) today holds three sibling blocks — Export (~line 943), Meta (~line 1160), Actions (~line 1259). This adds a fourth, **Check**, placed above Export: the checks are draft health, not export preflight. A retracted cite or an uncleared figure is a problem while writing, whether or not the draft is ever exported, so burying them inside Export hides them from everyone not currently exporting.

**Check** — facts and the action that refreshes them, always visible:
- Retraction/DOI summary line (retracted, soft, unchecked, missing_doi, doi_invalid, doi_unvalidated) — already computed by `GET /drafts/{ident}/retraction-status` in one walk, and already rendered as clickable count segments by the existing Alpine component, so this is largely moving markup.
- Draft-wide figure clearance roll-up.
- `check retractions` and `force`.
- `papers to fetch ▸` — the worklist of cited papers the corpus does not hold. A readiness signal, not an export.

Two scope collisions to resolve in the markup, because the right rail already has focus-scoped neighbours that look like these:
- **Figure clearance appears twice.** ~line 853 is the *focused chunk's* figure, with its add/clear controls. Check needs the *draft-wide* roll-up ("1 of 12 figures uncleared"). Same data, different scope — name the scope in both headings or they read as one being broken.
- **Cited sources appears twice.** ~line 760 lists the *focused block's* citations; Check covers the whole draft's cited set. Same treatment.

**Export** — everything that produces a file. At its top, one derived line joining it to Check: `blocked — 1 retracted cite, 1 uncleared figure`, with the per-check overrides `ignore_retractions` and `placeholder_figures` beside it; absent entirely when clean. This is not a duplicate of Check — it is one sentence of derived verdict against Check's full detail, and it clicks through to Check.

The overrides live here rather than in Check deliberately: Check states facts, Export is where the author decides to ship anyway. "Yes, I know, send it" is an export-time decision and is meaningless outside one. The cost is that a blocked export shows the verdict without the detail until you click; blocked is the exception, and when it happens the full panel is what you want anyway.

Four tabs, read as *what am I taking away*:
- **PDF**: `+ sources`, `DOI links`, `library links`, `footnote references` (today reachable only via reMarkable mode); actions `download` and `export →project`.
- **Word**: `+ sources` (as zip), `DOI links`, `library links`, citation-marks radio (plain vs EndNote Cite-While-You-Write fields; `?citations=endnote` already implements this). Also serve the EndNote XML library sidecar that `src/precis/export/endnote.py` generates but no route exposes; it is a separate artifact from the radio and a Word user typically wants both.
- **reMarkable**: `allow placeholder figures` and the send of *this draft* only. No link checkboxes — footnote mode is forced on in RM mode by design.
- **Papers**: `papers.zip`, `papers → reMarkable`, and `reading → reMarkable` (typeset reading editions of each source). These three export the *bibliography*, not the draft; two of them currently sit in the reMarkable group, which misfiles them. Collecting them here leaves the reMarkable tab holding only "send this draft to the tablet".

Option state is **shared across tabs, not per-tab**: switching tabs changes the output format only. Every option appearing in two panels means the same thing in both (`+ sources` is include-the-cited-papers either way; the link checkboxes are the same two booleans reaching the same two exporters). Only the EndNote radio, `footnote references`, and the tablet send targets are single-panel.

## Explicitly NOT in scope

- Exporter logic changes (PDF, Word, reMarkable rendering paths stay as-is).
- New options beyond `doi_links` and `library_links`.
- UI polish / animation / styling beyond layout cohesion.
- The cite-related bugs noted below (refs.bib `journaltitle`, endnote.py `journal`, and the arXiv/DOI prose guard).

## Acceptance criteria

- Check renders as its own Tools-pane block above Export, carrying the cite/DOI counts, the draft-wide figure roll-up, the retraction re-check and `papers to fetch`.
- Export carries the blocking verdict line and the two overrides; it shows nothing when the draft is clean.
- The draft-wide and focus-scoped figure/cited-source panels name their scope distinguishably.
- `doi_links` and `library_links` are threadable through web routes and honored by PDF and Word exporters; the UI exposes toggles for both.
- Four tab panes (PDF, Word, reMarkable, Papers) render format-specific controls only, with no duplication or orphaned options.
- Option state persists when switching tabs (shared, not per-tab).
- All three export formats produce correct output with the new controls in place (e.g., disabling `doi_links` removes the link words; reMarkable `allow placeholder figures` works as before).
- Tests cover state sharing and correct option plumbing to exporters.

## Target + blast radius

- **Web UI**: `src/precis_web/templates/smartdraft/view.html.j2` (split the Tools pane's Export block into Check + Export, tabs inside Export).
- **Routes**: `src/precis_web/routes/drafts.py` (thread `doi_links` / `library_links` through download endpoints).
- **Exporters**: `src/precis/export/latex.py`, `src/precis/export/docx.py` (honour the params; already do in principle; verify plumbing).
- **Tests**: cover state sharing and export correctness with the new controls.

Post-deploy: verify PDF and Word output with and without link words; verify reMarkable send targets; confirm retraction checks are applied consistently.

## Open questions / decisions log

Reto approved the four-block Tools pane (Check / Export / Meta / Actions) and the four export tabs on 2026-09-28. Check-above-Export is a weak preference, not a firm decision — the existing order puts Export first and there is no real cost to leaving it.

Related, all out of scope here:
- gripe 454753 — the export gate inconsistency (slice 1 of this item).
- gripe 454749 — MCP guard against raw arXiv/DOI identifiers in draft chunk prose.
- The refs.bib builder omits `journaltitle`, so every `@article` renders as "In: (2026)" with an empty venue; fix in flight at time of writing. `src/precis/export/endnote.py` reads the same `source["journal"]` field and has the identical bug.
