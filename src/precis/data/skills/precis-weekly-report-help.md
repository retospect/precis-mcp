---
id: precis-weekly-report-help
family: drafting
title: precis — build the weekly report as a draft and ship its PDF
summary: the weekly report is a draft with scaffold='report', owned by the week's todo and rebuilt weekly from live calls; its PDF comes only from a draft_export job — section order, the working call per number, plan accountability, house style
answers:
  - how do I build the weekly report draft?
  - how do I export the weekly report as a PDF?
  - which calls produce the week's numbers for the report?
  - what order do the report's sections go in?
  - how do I keep the report's plan accountable week to week?
flavor: runbook
status: active
applies-to: get/search/put/edit (kind='draft', 'job', 'quest', 'gripe', 'todo')
tags: [workflow, drafting]
kinds: [draft, job, quest, gripe, todo]
---

# precis-weekly-report-help — the weekly report, rebuilt from calls

The weekly report is a `draft` with `scaffold='report'`, bound to the
owning todo (`draft-of`) and rebuilt every week. Its PDF is **only** the
draft's export job — never a PDF made another way. Every number in the
report traces to a call that produced it, so the report can be rebuilt
from its own "reproduce" section.

## Start the week's report

One draft per week, bound to the week's todo:

```python
put(kind='draft', id='<slug>', project='<owning todo>', title='<week> — <title>')
edit(kind='draft', id='<slug>', scaffold='report')  # once, early — appends the section skeleton
```

The scaffold appends and never reorders, so scaffold once. When most
sections carry over, fork last week's draft instead and fix the
skeleton drift in the fork:

```python
put(kind='draft', copy_of='<last-week slug>', project='<owning todo>', id='<new slug>')
```

## Get the PDF — the draft_export job only

```python
put(kind='job', job_type='draft_export', parent_id='<owning todo>', params={'draft': '<slug>', 'doi_links': True, 'library_links': True})
```

Both link switches stay on for the circulated copy: a reader jumps from
a citation to its DOI or library record. The clean submission copy turns
both off ([[precis-draft-export-help]]). The job lands the PDF path in
its summary. A drifted cite blocks the export and names the chunk —
rewrite the passage to the hub's current sentence and re-export.

## Number the week — one working call per figure

```python
search(kind='job', view='count', args={'group_by': 'job_type', 'since': '<date>'})  # ticks and jobs by category; its "error heads" footer gives failure causes
search(kind='job', view='count', args={'group_by': 'state', 'since': '<date>'})
get(kind='quest', id='qu<N>', view='gaps')  # momentum line + gap rows for the quest state section
get(kind='quest', id='qu<N>', view='logbook', args={'since': '<date>'})  # the week's quest activity; 50-row cap
get(kind='gripe', id='/open')  # header line counts open gripes
```

Gripes fixed this week (origin × goal) has no call: the gripe store
cannot filter by close date [gr592737] — compile that table outside the
verbs and paste the rows in. The same holds for anything needing git
history or repo state (commits, release counts, build-lane position):
the verbs cannot see it, so compute it outside, paste the numbers, and
keep the "reproduce" section to calls only.

## Order the sections

Front to back, every week:

1. Executive overview — one page: state of the system, this week, the
   plan for next week as a list, Table 0 numbers. Opens with the
   scorecard (below).
2. Summary.
3. The pipeline in plain words.
4. State of the quest — facts, candidate table, ladder figure.
5. The week's finding(s).
6. Best-evidenced leads.
7. Compute and HPC — CPU and GPU utilisation against the goal of
   running owned compute at capacity. Full utilisation at a facility
   means we can queue, so write blockers as ours (image, submission
   lane), not theirs.
8. Gripes fixed this week (origin × goal).
9. The autonomous build lane.
10. Background ticks and jobs by category (the job count view).
11. Plan and issues filed in this run.
12. Reproduce — the exact calls from "Number the week" plus the export
    job.
13. Glossary.

## Hold the plan accountable

Every plan item in the report carries the handle it depends on — a
gripe, todo or ruling id in brackets — so the next writer can pull each
one. The next report's executive overview opens with a scorecard of the
previous plan, item by item: done (with the landing commit), moved (to
a new handle), or dropped (with the reason), stated before the new
plan.

## Write in the house style

- Every claim carries a handle in brackets — `[fi<id>]`, `[gr<id>]`,
  `[qu<id>]` — so the export links it into the web app.
- No hostnames in prose, ever.
- Never open a sentence with a lowercase identifier — write "The
  reaction engine, autocatpath, ...", or set the identifier in
  backticks.
- Every table that uses gate vocabulary carries a "Reading the table"
  legend under it explaining the symbols, and the glossary carries the
  same terms. The legend's heading is the verbatim phrase
  `Reading the table`.
- The exporter numbers figures but not tables: number tables by hand in
  order, and write no "Figure N." in captions.
- Figures render only at export — the draft shows the figure chunk; the
  PDF carries the rendered picture.
- Plain prose: no bold, no em-dashes, define a term on first use.

## See also

- [[precis-draft-help]] — the draft kind, scaffold classes, chunk grammar.
- [[precis-draft-export-help]] — the draft_export job, blocking checks, audiences.
- [[precis-quest-help]] — the quest views: gaps, logbook, tree.
- [[precis-gripe-help]] — the /open and /wontfix gripe lists.
- [[precis-job-help]] — the job count view: group_by, since, error heads.
