---
status: draft
title: reconcile `realized-by` (cad, mig 0156) with se's `set_binding`
prio: medium
model: opus
---

# Two spellings of "this design is that component"

Surfaced in cross-session coordination 2026-09-05 (cad ↔ se tracks),
right after both shipped their halves independently:

- **cad `realized-by`** (mig 0156, `relations` row + inverse
  `realizes`): a link design → procurable `component` ref. Written two
  ways — auto-synced from `part <family>:<code>` catalog lines
  (`links.meta.catalog` marks managed rows) and hand-authored via
  `link(kind='cad', rel='realized-by', target='component:…')`. Many
  candidates legal.
- **se `set_binding`**: se's plugin-local binding of a member/fastener
  to a `component` row (name-keyed, plugin tables — deliberately not a
  `links` edge; se relations stay plugin-local per the 2026-09-05
  agreement).

They agree semantically and diverge mechanically. That's fine at two
consumers; it stops being fine at three — a BOM/cost/lead-time consumer
that wants "everything this artifact resolves to" would have to know
both spellings.

## Decide before a third consumer appears

Options (not pre-judged): (a) se's binding additionally emits a
`realized-by` link (link = the queryable projection, plugin table stays
authoritative); (b) a read-side union view; (c) status quo, documented.
Owner: whichever track grows the next consumer. se's mirror note:
`se-off-the-shelf-fabrication.md`.

Related, same session: three independent ISO fastener tables
(cad `catalog.py` — store-free by design; `component_series.json`;
`fit_classes.json`) are drift-guarded by
`tests/test_standards_table_agreement.py` (se-authored; deliberately
imports cad's private dicts — restructuring those tables reds it, which
is the point). Consolidation is a live option nobody has decided.
