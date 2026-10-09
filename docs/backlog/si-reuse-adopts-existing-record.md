---
status: draft
pillar: platform
prio: medium
---

# SI fetch: adopt an existing role-main record instead of silently skipping

Found 2026-10-09 on pa2069 (Nasibulin 2007, "A novel hybrid carbon
material"). Its SI fetch of 2026-10-08 reports `si_found: 1, si_fetched: 1`,
yet the paper has no supplement child and no `part-of` link.

Cause: the fetched SI PDF's sha was already held by pa2615 (wang22c), a
hand-ingested copy of the same supplement from before the SI model existed,
with `pdf_role = 'main'`. `precis.ingest.add._reuse_supplement` refuses to
link a role-main ref ("would turn a real paper into someone's SI") and only
logs a warning. The parent's counters still say fetched, so the gap is
invisible from the paper overview and from `si_fetched` sweeps.

## Wanted

1. When the reuse path meets a role-main ref whose title says supplementary
   / supporting information (the `_si_overview_lines` vocabulary) and that
   holds no DOI of its own, adopt it: set `pdf_role = 'supplement'`, add the
   `part-of` link with `meta.role = 'supplement'`, set `meta.si_parent`
   (`source: 'reuse'`), retitle. Same effect as the manual
   `edit(kind='paper', args={'supplement_of': …})` verb.
2. Otherwise record a named skip on the parent (`si_skipped` entry with
   `reason: 'sha_held_by_main_ref'` and the ref id) so the overview shows
   "supplement exists, not ingested" instead of nothing.
3. A one-off sweep: parents with `si_fetched >= 1` and no supplement child;
   expect the 24 supplement-titled role-main records (7 without DOI) to be
   the candidates.

## Related

`docs/backlog/ref-2615-is-a-mis-bound-record.md` (the wang22c record
itself; repair = declare it `supplement_of` nasibulin07a and give it a
non-mining handle). `docs/backlog/si-fetch-reliability-r15.md`.
