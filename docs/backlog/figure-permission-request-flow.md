---
status: draft
title: Figure permissions — close the loop from ledger to actual request
prio: normal
---

# Figure permissions — close the loop from ledger to actual request

## Motivation / why

A `third_party` figure cannot be saved without a permission record —
`precis/handlers/draft.py` enforces that at the single validation point
both the web form and the tool surface route through, which is the right
place for it. But the record is a **ledger of something you did
elsewhere**, not a workflow:

- nothing generates or tracks the request to the publisher;
- `status` is free text, so "pending" means whatever the person typing it
  meant;
- `expires_at` is stored and then never looked at again — no alert, no
  badge, nothing at export;
- `required_credit` is stored and never inserted anywhere. A caption
  missing the exact wording the publisher demanded is a licence breach,
  and today the only thing standing between the draft and that breach is
  the author remembering.

The asymmetry is the problem: the gate is strict at *save* and silent
forever after. A figure cleared in March with a six-month window exports
into a paper in November with no complaint from anything.

## In scope

Pick up the loop where the ledger drops it. Roughly in value order:

1. **Credit enforcement at export.** A `third_party` figure whose
   `required_credit` string is not present in its caption blocks the
   export the way a retracted cite does — same shape, same deliberate
   override (`precis/export/retraction.py` is the pattern to copy, not
   to extend).
2. **Expiry visibility.** A figure whose `expires_at` has passed, or is
   near, surfaces where it can be acted on — the clearance badge, and
   the alert surface for a draft that is otherwise ready to export.
3. **A request artifact.** Generate the permission request itself from
   what is already known (publisher, source paper, figure, intended
   scope) as an `email` ref, so the ask and its answer live next to the
   figure instead of in someone's sent mail. Sending stays manual.
4. **A closed `status` vocabulary** replacing free text, so "have I
   cleared everything in this draft" is a query and not a read-through.

Split into separate items if 1 ships without the rest — 1 is
independently valuable and independently shippable.

## Explicitly NOT in scope

- **precis does not send mail to publishers.** Item 3 drafts the
  request; a human sends it. An automated outbound rights request under
  an author's name is not a thing this system should do unasked.
- No scraping of publisher permission portals, no RightsLink
  integration.
- No retroactive validation of existing figures at migration time —
  enforcement is at export and at edit, so old records surface when
  someone next touches them rather than in a bulk red-flag sweep.
- Not a licence-compatibility engine. Storing what the publisher granted
  is in scope; deciding whether it covers your use is not.

## Acceptance criteria

- A draft with a `third_party` figure whose caption omits
  `required_credit` fails export with a message naming the figure and
  the exact missing string; the deliberate override path works and is
  recorded.
- A figure past `expires_at` is visibly distinct on the clearance badge.
- `own_graph` / `original` figures are unaffected on every path — no new
  friction for a figure we made.
- A `third_party` figure with a complete, in-date record and its credit
  in the caption exports byte-identically to today.
- `src/precis_web/manual/03-figures-and-permissions.md`'s "What precis
  does not do" section is cut down to what is still true, in the same
  commit.

## Target + blast radius

`precis/handlers/draft.py` (figure meta validation),
`precis_web/routes/drafts.py` (`/figure`, `/figure/{handle}/permission`),
`precis_web/templates/drafts/_figures.html.j2` (clearance badge),
`precis/export/` (the new gate). Item 3 additionally touches the `email`
kind. A closed status vocabulary (item 4) is the only part needing a
migration.

## Open questions / decisions log

- Credit matching: exact substring, or normalized (whitespace, smart
  quotes, unicode dashes)? Exact will produce false blocks on a
  copy-paste from a publisher PDF; normalized risks passing a caption
  that is subtly wrong. Lean normalized, with the diff shown on block.
- Does the export gate belong with the retraction walk (one pre-export
  clearance pass) or as its own? Prefer one pass, two reasons, so a
  blocked export reports everything wrong at once instead of one problem
  per attempt.
