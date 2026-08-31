---
status: draft
title: PDF extraction drops the µ in units, turning V/µm into V/mm
prio: medium
---

# Extraction drops `µ`, silently changing a unit by 1000×

`pc35564` (pa494, field emission of carbon NanoBud / nanographite films)
reads:

```
Threshold fields corresponding to the current density 10 mA/cm2 for these
films are in the range of 1-2 V/ mm.
...
threshold field values for these films were less than 1 V/mm usually.
```

The stray space in `V/ mm` is the tell: the source is `V/µm` and the `µ`
was lost in extraction. Field-emission threshold fields for CNT cathodes
at 10 mA/cm² are ~1-10 V/µm; `1-2 V/mm` is 0.001-0.002 V/µm, three orders
low and physically implausible.

## Why this is worse than a cosmetic typo

It survives verification and **poisons the provenance record**. Verifying
fi269548 on 2026-08-30 returned the right verdict for the wrong reason:

> "...threshold fields less than 1 V/mm (which is 0.001 V/µm, well below
> the claimed 1 V/µm)"

The hub claims "usually below 1 V/µm" — a faithful transcription of the
true source. The verifier read the corrupted chunk, computed a value
1000× smaller, and concluded *supports* because 0.001 < 1. The verdict is
correct; the recorded `support_reason` is not, and that text is now
stamped onto `links.meta` (link_id 1600554) as durable provenance.

The dangerous shape is the near miss: had the claim been stated as a
lower bound, the same corruption would have produced a confident
*contradicts*, and `check_contradicts` hard-blocks a hub at mint.

## What to look for

Any unit where `µ` is the only distinguishing character: `µm`/`mm`,
`µA`/`mA`, `µs`/`ms`, `µV`/`mV`. A corpus sweep for `/ mm`, `/ m`, and
bare `V/mm` in extracted chunks would size the problem — the stray space
is a usable signature, but the clean `1 V/mm` case above shows it is not
always present.

Note the numeric-notation lint cannot catch this: `ascii-micro` in
`_BLOCKING_LINT_CODES` guards *claim sentences* an author writes, not the
extracted passage text underneath them. This is an ingest-side defect.

## Related

`docs/backlog/nanobud-claim-remediation.md` — found while stamping
Phase 2 verdicts. The hub itself is correct and was stamped; only the
underlying chunk is wrong.
