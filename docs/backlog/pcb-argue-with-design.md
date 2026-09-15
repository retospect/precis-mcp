---
status: ready
title: pcb — argue with the design by clicking devices into a text box
prio: high
model: opus
---

# pcb: argue with the design

Reto, 2026-09-15, after eyeballing the freshly-minted `ewod-dogfood-1`:
"There should be a text box to argue with the design, as a whole or by
clicking on a [pad] or chip. I don't think we add a route or place
button. But clicking on a device should allow us to … get its handle
into the text box so we can click on the thing, type something, click on
another thing, type something, so the llm can fix problems, and I can be
super specific."

The se kind shipped the neighbouring feature the day before (`c46b91cd`,
`se-topology-cloud-and-surface-notes.md` slice 2): select a block in the
3D viewer → comment box → LLM rewrite → `se_notes` interview note. This
is the pcb counterpart, and the interaction Reto describes is *better*
than what se shipped — one text box that accumulates handles as you
click, instead of one box per selection. Build it here; the se back-port
is its own item (`pcb-argue-backport-se.md`).

## Why handles, not a picker

The value is precision: "this pad, not that one" typed while looking at
the board, in a form an agent can resolve without guessing. A
selection-scoped comment box can only ever carry ONE anchor, so a
multi-part argument ("R_BLEED is on the wrong side of J_HV, and the
plaza via at R4C4 is missing") has to be split into several notes that
lose their relation. Inserting the handle as *text at the cursor* means
one note, several anchors, in the user's own sentence order.

## Anchor grammar (pcb)

Mirrors se's block/port/measure convention (and gr335242 comment 2's
anchor-grammar extension). **One notation, no synonyms** — every
clickable thing resolves to exactly one textual handle, inserted
verbatim into the text box:

| clicked | handle | resolves to |
|---|---|---|
| part body / its refdes label | `U_TEMP` | `pcb_instances.refdes` |
| pad (incl. a generated array cell) | `U_TEMP.3`, `ARR1.R3C4` | refdes + pin |
| net (a route/ratline) | `net:HV_RAIL` | `pcb_nets.name` |
| board feature | `feature:outline` | `pcb_features` row (`ftype` ∈ `mounting_hole\|fiducial\|testpoint\|keepout\|outline`) |
| nothing (whole-design argument) | *(no handle)* | the design |

A generated array cell needs **no separate notation**: `ewod_pad_array`
already names its pins `R{r}C{c}` under the array's own refdes, so
`ARR1.R3C4` *is* the `refdes.pin` row. A plaza via likewise resolves to
the electrode pin it serves (one via per electrode, spec rule) — there
is no `feature:plaza`, because plazas are not `pcb_features` rows and
`ewod_pad_array` never populates `GeneratorExpansion.features`.

Handles are *name-keyed text*, resolved at read time — a dangling
anchor is a read-time report, never a write-time error (se_notes'
established rule, `precis_se` migration 0005's own comment).

## Target + blast radius

- **Target**: the human-facing board page `/pcb/{slug}` and its board
  render; a new notes store for the pcb kind.
- **Renderer**: `src/precis/pcb/gerber_view.py::render_fab_svg` — NOT
  `svg.py`. `routes/pcb.py::pcb_board_svg` hardcodes `level='fab'`,
  which dispatches to `PcbHandler._render_fab_svg` → `render_fab_svg`;
  `svg.py::render_board` is only reached via `level='board'`, which
  this page never requests. `data-handle` goes on `render_fab_svg`'s
  flash/stroke/region emitters (`_flash_svg`/`_flash_hit_target` already
  give pads a hover `<title>` — same elements, one more attribute).
  Leave `svg.py` alone this slice.
- **Blast radius**: `pcb_features`/`pcb_instances`/`pcb_nets` are read
  only. The SVG gains attributes on existing elements — no geometry,
  no layout change, so every pinned gerber/SVG export assertion should
  be unaffected; `tests/test_pcb_dead_exports.py` and the fab-export
  byte checks are the canaries. New table + new route are additive.

## Slice 1 — capture

1. **Handles in the fab SVG.** Add `data-handle="…"` per the grammar to
   pad flashes, part bodies/labels, and feature shapes in
   `render_fab_svg`.
2. **Cross-document click wiring — the load-bearing detail.** The board
   pane embeds via `<object type="image/svg+xml">` (`pcb/detail.html.j2`)
   *deliberately*, so the SVG's own layer-toggle legend script runs as
   its own document. A `[data-handle]` listener on the host page will
   therefore **never fire**. Both documents are same-origin, so the host
   page reaches in directly:
   ```js
   objEl.addEventListener("load", () => {
     const doc = objEl.contentDocument;      // same-origin: readable
     doc.addEventListener("click", (ev) => {
       const el = ev.target.closest("[data-handle]");
       if (el) insertHandle(el.dataset.handle);
     });
   });
   ```
   No `postMessage` bridge, no inlining the SVG, no touching the
   legend script. Guard `contentDocument === null` (blocked/not-yet-
   loaded) by degrading to a text box that still works for
   whole-design arguments — a click that does nothing is acceptable;
   a page that breaks is not.
3. **One text box** on `/pcb/{slug}`, always present (not
   selection-gated — a whole-design argument needs no click). A click
   inserts the handle at the caret, space-padded, and returns focus to
   the box. No modifier keys, no multi-select state: the text box IS
   the state.
4. **Submit → a pcb note, verbatim.** New `pcb_notes` table (core
   migration **0163** — verified free, 0162 is design-core's),
   column-for-column `se_notes`: `ref_id, name, kind, body, re,
   about jsonb, origin, created_at, retired_at`. `about` gets the
   handles parsed out of the submitted text (they stay inline in the
   body too — the sentence is the point); `kind` defaults to
   `question`; `origin='user'`. Reuse `precis.utils.notes.NoteSpec`
   (already lifted out of se for exactly this).
5. **New route** `POST /pcb/{slug}/note`, mirroring
   `POST /se/{slug}/note` (`routes/blocktree_view.py:901` — ambient
   store access, no auth/CSRF layer beyond the page's own, so there is
   nothing extra to match), returning the rendered note list. Read
   surface: `get(kind='pcb', id=…, view='notes')` (no collision with
   pcb's existing view roster) + notes rendered under the board.

**Decision — NO forced AI rewrite** (differs deliberately from se slice
2, veto-able): se's rewrite exists because a spoken one-line comment
needed sharpening into intent. Here the handles do that work and Reto's
stated goal is "I can be super specific" — rewriting would blur exactly
the precision he is buying. Store what he typed. (An *optional*
rewrite-suggest button is a later nicety, never the default path.)

**Non-goal, explicit (Reto):** no place or route button on the page.
Actions stay in the MCP/job surface; the page is for arguing, not
driving. A submit button for the argument itself is not that.

## Slice 2 — the LLM acts on the argument

An argument note is an instruction, not just a record: "so the llm can
fix problems". A `pcb_argue` job type reads open argument notes on a
design, resolves their anchors, and answers each in place — following
`gr335242` comment 1's playbook, which already classified the three
canonical argument forms and is kind-agnostic:

- **change request** → propose the edit (`origin='proposed'`, human
  accepts) — e.g. "R_BLEED should be bottom-side".
- **interference/geometry concern** → run the real machinery (DRC on
  the named pair, clearance probe, courtyard check) and answer with
  numbers — e.g. "these two pads look shorted".
- **verification question** → run what exists and answer HONESTLY when
  the question exceeds the model, naming the backlog item it waits on —
  never a confabulated yes.

Every answer lands as an `answer`/`decision` note anchored to the same
handles, so the argument thread lives on the design.

**Wiring this slice needs, none of it free** (the vet caught all four):
`PcbHandler`'s `KindSpec` must set `can_own_jobs=True` (it never does
today — defaults `False`; `handlers/figure.py` is the explicit-`True`
precedent) so the job can be parented on the design it argues about;
a params schema; an executor lane assignment; and a **prod
`service_config` row**, without which the job type ships dark and
never runs — that exact gap has bitten this project twice (disputes
conflict-search, and the nm→se `se_propose_atomic` rename).

## Acceptance criteria

1. On `/pcb/ewod-dogfood-1`, clicking an electrode pad, the HV507 sink
   body, and a net in turn builds a single text box containing all
   three handles interleaved with typed prose — verified in a real
   browser at 1280×800 (screenshot), because the cross-document
   listener is exactly the class of thing unit tests cannot prove.
2. Submitting that text stores ONE `pcb_notes` row whose `body` is
   byte-identical to what was typed and whose `about` lists the three
   handles.
3. A whole-design argument (no handles) submits and stores with empty
   `about`.
4. The note appears on the detail page and in
   `get(kind='pcb', id='ewod-dogfood-1', view='notes')`.
5. A handle naming a since-deleted part reads as a dangling-anchor
   report, not an error.
6. Fab export byte assertions unchanged (`data-handle` is additive).
7. No place/route button exists on the page.

## Tests

- Anchor grammar: each clicked kind → expected handle; dangling handle
  reads as a report, not an error.
- `data-handle` present on pads/parts/features in `render_fab_svg`
  output for a realized board and for a generated array (the
  `ewod-dogfood-1` shape).
- `POST /pcb/{slug}/note`: body stored verbatim, anchors parsed into
  `about`, `origin='user'`, bad slug 404s, whole-design note accepted.
- Notes render on the detail page and via `view='notes'`.
- One Playwright-style smoke for the click-inserts-handle path across
  the `<object>` boundary (acceptance criterion 1) — the only part of
  slice 1 not provable without a browser.
