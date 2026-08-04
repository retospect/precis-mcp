---
status: draft
title: Trust surfaces for unverified claims — export marking, unacquirable override, editor badges
model: opus
blocked-by: finding-acquisition-mode
---

# Trust surfaces for unverified claims — export marking, unacquirable override, editor badges

## Motivation / why

`finding-acquisition-mode` gives a claim a life *before* its evidence
is verified (`STATUS:acquiring`, and the pre-existing `tracing`). We
don't trust a claim until it is actually resolved — and that distrust
must propagate to every surface a human sees, or the acquiring state
quietly launders unverified claims into finished prose. Split out of
`finding-acquisition-mode.md` per its 2026-08-04 readiness review
(disjoint file set, independently testable, one-way dependency:
substrate first).

Human-facing vocabulary (decided, Reto 2026-08-04) collapses the
machine states to two labels:

- **unverified — source pending** (`acquiring`, `tracing`): the author
  asserts it; the system hasn't confirmed it. `tracing` findings get
  the same treatment as `acquiring`-born ones — the reader-facing
  question "is this verified?" doesn't care which path got here.
- **unsupported — verification failed** (contradicted / none-fit after
  grounding ran): the paper arrived and does not say what the claim
  says. Renders louder than pending.
- `dead_chain(unacquirable)` buckets as unverified-pending-with-a-note
  ("no OA copy obtainable; hand-download queued"), *not* unsupported —
  nothing contradicted the claim.

("speculative" rejected as the default label — it mis-attributes the
doubt to the author; reserved for a possible future author-chosen
conjecture marker.)

## In scope

### 1. Export marking (docx / latex)

**Export always works, always marks** (decided — no refusing/strict
mode; marking *is* the mechanism):

- An unverified-backed citation renders with a visible inline mark
  (e.g. `[unverified: Smith 2021 — source pending]`) plus an
  end-matter "Unverified claims" list (claim, state, what it's
  waiting on).
- An unsupported-backed claim renders louder:
  `[UNSUPPORTED — cited source does not back this claim]`.

### 2. Author override for unacquirable sources

A print-only / undigitized source is legitimately citeable even when
no digital copy is obtainable. An explicit per-claim override
suppresses the unverified mark:

- Stored durably on the finding
  (`meta.unacquirable_override = {by, at, note}`).
- **Recorded in the export record**: at export time, a `ref_events`
  row is appended to the draft ref (existing `append_event` machinery
  — no schema change) listing the overridden claims, so the trust
  decision is visible in the audit trail, never silent.

### 3. Editor badges (smartdraft)

The live surface is the **already-built** per-paragraph review-status
indicator (`docs/proposals/smartdraft-review-status-ui.md`, shipped:
four-state grey/hollow-blue/green/amber + tooltip matrix,
`src/precis_web/routes/smartdraft.py` +
`templates/smartdraft/view.html.j2`). This proposal must integrate
with it, not collide: **open question below (fold-in vs sibling
badge) must be decided before `ready`.** Either way, a paragraph
leaning on an unverified/unsupported claim shows the state live, long
before export.

## Explicitly NOT in scope

- The substrate itself (`STATUS:acquiring`, mint mode, chase bridge,
  give-up) — that is `finding-acquisition-mode`.
- Any refusal/strict export mode (explicitly rejected).
- W2/W3 workflow templates (repair / review drafts) — separate
  follow-on; this proposal only renders states, it doesn't drive
  verification.
- Blocking or altering draft *editing* — trust surfaces are
  read/render-time only.

## Acceptance criteria

1. Exporting a draft citing an `acquiring`- or `tracing`-backed claim
   yields the inline unverified mark and the end-matter list, in both
   docx and latex paths.
2. An unsupported-backed claim renders the louder mark, visually
   distinct from pending.
3. A finding with `meta.unacquirable_override` renders as a clean
   citation, and the export appends a `ref_events` row on the draft
   naming the overridden claim(s), author, and timestamp.
4. A `dead_chain(unacquirable)` claim without override renders as
   unverified-pending-with-note, not unsupported.
5. The smartdraft paragraph indicator reflects
   unverified/unsupported state per the decided integration shape,
   without regressing the shipped four-state review behavior (its
   existing tests pass unmodified).
6. `STATUS:established`-backed citations render byte-identically to
   today (no mark, no event row).

## Target + blast radius

- `src/precis/export/docx.py`, `src/precis/export/latex.py` — citation
  render + end-matter list + override handling + export-record event.
- `src/precis/handlers/finding.py` — `meta.unacquirable_override`
  write path (a small authenticated edit door or meta field on
  `edit`/`tag`; exact verb decided at implementation).
- `src/precis_web/routes/smartdraft.py`,
  `templates/smartdraft/view.html.j2` — badge integration.
- Tests across all of the above.

## Open questions / decisions log

- **Decided (Reto, 2026-08-04):** always-mark, no refusal mode;
  override recorded, never silent; vocabulary as in Motivation.
- **Decided (2026-08-04):** export record = `ref_events` row on the
  draft ref, written at export time via existing `append_event`.
- **Open (blocker for ready):** badge integration shape — fold
  unverified/unsupported into the existing four-state review indicator
  (a fifth/sixth state) vs a sibling badge alongside it. Needs a look
  at the shipped tooltip matrix + state semantics before deciding.
- **Open:** exact verb/door for setting `unacquirable_override`
  (edit meta vs a dedicated tag axis) — decide at implementation with
  the handler in front of us.
