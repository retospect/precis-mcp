---
status: implemented (slices 1-2 shipped 2026-08-04; slice 3 rides hub_refine)
title: Claim-page rendering + smartdraft claim interaction (diamond↔rail, docked claim pane, paper-at-position)
model: sonnet
---

# Claim-page rendering + smartdraft claim interaction

From Reto's 2026-08-04 review of `dr173020` on `/smartdraft/173020` and the
`/claim/fi…` pages. Six fixes, three slices. Anchors verified against the
tree at authoring time.

## Already decided (shipped with this worktree, not part of this proposal)

Claim **sentences are plain text** — UTF-8 sub/superscripts (`C₆₀`,
`g-C₃N₄`, `cm²/Vs`), never TeX fragments. Encoded in the
`precis-taproot-help` rubric and extract-prompt rule 4 (`taproot/canon.py`);
the nanobuds retitle batch already conforms. Consequence for the UI: the
claim **title** needs no math engine — only legacy titles do (see slice 1
sweep). Grounding **quotes** are verbatim paper text and legitimately
contain TeX + markdown tables — those need real rendering.

## Slice 1 — claim page rendering (`/claim/<head>`)

1. **Stop destroying table structure.** `claim_render.py` whitespace-collapses
   every grounding quote (`" ".join(text.split())`, ~line 150) before the
   template escapes it (`claim/view.html.j2:86`) — a markdown table arrives
   as one inline run of pipes (Reto's fi191167 symptom "tabular not rendered
   right"). Fix: keep newlines; render the quote through the same markdown
   pass the reader uses, so tables become `<table>`. Clamp by chars *after*
   render, or by rows for tables.
2. **Math in quotes.** Add KaTeX (auto-render on `$…$`/`$$…$$`) to
   `base.html.j2` (currently only htmx + Alpine, lines 11-12), scoped to the
   grounding-quote container. Claim titles stay out of scope by policy.
3. **Legacy-title sweep.** One-time fleet query for `refs.title LIKE '%$%'`
   on `TAPROOT:claim` hubs → retitle via `refine_claim_sentence` to UTF-8
   (aliases keep old pub_ids resolving). Small; can ride any ops session.
4. **Verify the "only one chunk" report.** The renderer already lists ALL
   supporters and ALL distinct grounding chunks (`claim_render.py:194-207`,
   dedup by `source_handle` at `:207`) — fi191167 has three. Either the ★
   print-set grouping visually buries the non-starred ones or dedup collapses
   them; reproduce on `/claim/fi191167` and fix the grouping so every
   passage is visibly listed with its role label.

## Slice 2 — smartdraft reader interaction (`/smartdraft/<id>`)

5. **Diamond ↔ Claims-rail sync.** The prose ◆ (emitted by
   `linkify.py::_render_claim_hub`, ~:417) and the rail chip
   (`smartdraft/view.html.j2:501-513`) already share the claim `head` — but
   no wiring exists. Add `data-claim-head` to both; a small Alpine/JS
   delegate highlights the counterpart on hover and scrolls it into view on
   click, both directions.
6. **Docked, scrollable claim pane** (the "4th column" ask). Recommend a
   right-rail docked panel over a permanent 4th grid column: clicking a ◆ or
   a rail chip htmx-loads the existing `/preview/claim/<head>` fragment into
   a persistent, scrollable "Claim" panel at the top of the right rail
   (reusing the popover card content, not the transient hover behavior);
   sticky until closed or another claim is clicked. Plain click stops
   opening a new tab (hook the reader delegate at `view.html.j2:997`;
   middle/ctrl-click keeps native new-tab). If it proves too cramped, the
   grid is one line to change
   (`view.html.j2:204`, `lg:grid-cols-[28%_44%_28%]`) — promote the panel to
   a real column then, not speculatively.
7. **Paper opens at position, in a reusable window.** Cite anchors already
   `target="_blank"` (`linkify.py:296`) but land at the paper top. Change:
   (a) carry the cited chunk in the href (`?focus=<pc-handle>`) so the paper
   reader scrolls to the passage; (b) use a *named* target
   (`target="precis-paper"`) so successive clicks reuse one side window
   instead of spawning tabs — side-by-side manuscript/paper reading.

## Slice 3 — grounding-depth policy (discussion capture, needs Reto's call)

Abstract-only grounding (fi189527) is fine for definition/existence claims —
the abstract is where a paper states "we constructed X" most cleanly. For
measurement/mechanism claims the abstract's numbers are summaries; policy:
at least one grounding chunk must *contain the claim's specifics*, and the
`hub_refine` dark pass gains a step — for hubs whose only grounding is an
abstract/intro chunk, search inside the paper for the body passage and
attach it as a second corroborator. No schema change; extends the existing
`hub_refine` design in `precis-taproot-help`.

## Effort

Slice 1: 1-2 (coder) — render-path only, plus the KaTeX include.
Slice 2: 2-3 (coder) — JS delegate + linkify anchor attrs; no backend.
Slice 3: rides the existing `hub_refine` follow-on; design-only here.
