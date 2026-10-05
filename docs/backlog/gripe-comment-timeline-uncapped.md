---
status: ready
pillar: platform
title: cap the gripe comment timeline on a bare get() — the links section already is
prio: high
---

# `get(kind='gripe')` renders every comment, uncapped

`GripeHandler._render_one` (`src/precis/handlers/gripe.py::GripeHandler._render_one`)
walks every chunk on the ref and appends **every** `gripe_comment` — no cap,
no pagination, no summary mode — on every single `get`.

The identical failure was already found and fixed one file over, for the
*links* section: gr311344 / gr311679 capped it at `DEFAULT_LINK_ROW_CAP`
because "an unbounded links section on a heavily-linked numeric ref (a
long-lived quest/gripe can accumulate thousands of links) rendered every row
on the request thread, hanging a bare `get()`"
(`src/precis/handlers/_numeric_ref.py`, the `_render_links_section` callsite).
The comment timeline has the same shape on the same rows and was never capped.

## Why it matters (surface-review pass #1, 2026-09-29)

`get(kind='gripe')` is **30,038 of ~37k** calls in a 5-day window — roughly
80% of all traffic on the MCP surface — and 27,680 of those are fleet-side
(`fix_gripe` / `diagnose_gripe` / `doctor_tick` jobs), not interactive.

Job transcripts show the cost is not "506 open gripes read once". It is the
same ~10–15 chronic `STATUS:triaged` trackers (346813, 245505, 347576,
366638, 440098, 366639, 347577 …) re-fetched **in full** across six-plus
separate doctor-tick sessions in the window. Those are precisely the gripes
with the deepest comment histories, because chronic trackers accumulate
comments by recurring — observed renders 5,470 B / 11,934 B / 14,168 B /
17,725 B / 19,491 B / 20,426 B / 21,399 B, against a scoreboard `get` p95 of
13,906 B.

Measured mean over payload-bearing calls: **4,716 B** (n=645, local +
job-transcript corpora).

**Fleet cost is a PROXY, not a measurement.** `tool_calls` carries no payload
by design (migration 0133), so applying that local mean to the 27,680 fleet
calls gives ≈130 MB ≈ 33M tokens over 5 days. Treat as an estimate. It is
more likely a floor than a ceiling: fleet traffic skews toward the chronic
trackers at the *top* of the observed size range, not a random gripe. See
`mcp-surface-economy.md` for the ledger `result_bytes` change that would
settle it.

## Generalize it — do not add a second cap constant beside the first

The tempting fix is `DEFAULT_COMMENT_ROW_CAP` next to `DEFAULT_LINK_ROW_CAP`.
That is the wrong shape: it makes "is this section capped?" a per-handler
decision that the next child-collection render will forget in exactly the way
the comment timeline did. Links were already capped for this precise reason
(gr311344/gr311679) and the lesson did not transfer, which is the evidence
that a convention-to-copy is not enough.

**The real defect is unbounded child-collection renders as a class.** Extract
one capped-section renderer that every ref render composes — it takes the
collection, a cap, a priority order, and emits the overflow line pointing at
the full view. Then:

- the links cap becomes its first caller (behaviour unchanged),
- the comment timeline becomes its second,
- and the audit question becomes mechanical: grep for child-collection
  renders that do *not* route through it. Today there is no way to ask that
  question except by reading every handler.

So: `get(kind='gripe')` renders body + the last N comments with a withheld
count and `view='log'` as the opt-in — but implemented *through* the shared
renderer, not beside it.

**Sequencing.** This is a prerequisite for `singleton-id-no-batch-form.md`: a
batch `get` of 50 gripes with uncapped timelines is worse than the loop it
replaces. Cap first, batch second.

## Explicitly NOT in scope

- Changing what `view='log'` returns — it should stay the full history.
- Touching the links cap.
- Any change to gripe write paths.

## Acceptance criteria

- A bare `get(kind='gripe', id=N)` on a gripe with 50+ comments renders a
  bounded body and states how many comments were withheld and how to get
  them.
- `view='log'` (or the chosen opt-in) still returns the complete timeline.
- A test pins the cap on a synthetic gripe with more comments than the cap —
  the regression that this item exists to prevent.

## Open questions

- What is N? The links cap is the obvious precedent to copy rather than
  re-derive.
- Newest-N or oldest-N? A doctor tick re-reading a chronic tracker probably
  wants the newest; the body plus first comment is usually the original
  diagnosis. Worth checking one real doctor-tick transcript before choosing.

## R14 checked implementation contract

Confirmed open/ready at origin/main ab90f225a. Current `view='log'` is the
audit event log, not comment chunks: preserve it and add `view='comments'`
for complete chronological body/comment history. Bare get retains the body
and newest 20 comments in chronological order. Twenty is the current links
precedent; newest comments retain current diagnosis on chronic trackers.
One shared capped-section renderer owns selection and overflow for links and
comments; links retain byte-identical formatting/order/cap. This bounds rendered
comment count, not individual body/comment length or DB chunk retrieval.
Regression: synthetic 55-comment ref, exact boundary/no overflow, full comments
read, legacy title fallback, existing links suite and unchanged audit log.
No schema/write-path/version changes; coordinator owns release/version gate.
