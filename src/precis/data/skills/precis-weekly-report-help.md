---
id: precis-weekly-report-help
family: drafting
title: precis — build the weekly report draft and export its PDF
summary: weekly runbook — the report is a draft with scaffold='report'; every number comes from one runtime call (job counts, quest gaps/logbooks, the /done closure window, the /open header); the PDF arrives only through a draft_export job; plan items carry dependency handles and the next issue scores the previous plan
answers:
  - how do I build this week's report?
  - how do I export the weekly report to PDF?
  - where does each number in the report come from?
  - how do I keep the plan section accountable week to week?
flavor: runbook
status: active
applies-to: get/search/put/edit (kind='draft'); search (kind='job'); get (kind='quest','gripe'); put (kind='job')
tags: drafting, workflow
kinds: draft, job, quest, gripe, todo
parent: precis-draft-help
---

# precis-weekly-report-help — build the weekly report

The weekly report is a living `draft`, rebuilt every week by the same
recipe. This skill is that recipe. What changes weekly — this week's
quest numbers, the "improve next week" list and its open gripes, the
next-week plan — stays in graph memory; do not bake it into standing
text. Anything that needs git history or worktree state is not part of
the recipe either: the report is built from runtime calls only, and a
number with no call behind it does not go in.

## Scaffold the report once, as a draft with scaffold='report'

```python
edit(kind='draft', id='<slug>', scaffold='report')
```

Scaffolding appends the section skeleton after whatever is already
there and never overwrites — scaffold once, early; a second scaffold
run duplicates the skeleton. The draft kind itself (chunks, prose
rules, search, review) is [[precis-draft-help]].

## Export the PDF only through a draft_export job

The report's PDF never comes from a local typesetting run; it comes
from a job:

```python
put(kind='job', job_type='draft_export', parent_id=<project-todo-id>, params={'draft': '<slug>', 'doi_links': True, 'library_links': True})
```

`parent_id` is the report's owning todo. The job resolves citations,
cross-references and abbreviations itself; when the export refuses
(drifted cites, uncleared figures), fix the draft and re-run the job
rather than working around it. Export params and the blocking gates
are [[precis-draft-export-help]].

## Keep the sections in this order

1. Executive overview — programme state, this week in one paragraph,
   the plan list, Table 0 (the scorecard, below).
2. Summary.
3. The pipeline in plain words.
4. Quest state.
5. The week's findings.
6. Leads.
7. Compute and HPC.
8. Gripes fixed.
9. Build lane.
10. Ticks and jobs, by category.
11. Plan and issues.
12. Reproduce.
13. Glossary.

The order is fixed so a reader can find a section by position. Do not
reorder or drop a section because the week was quiet — write "no
change" instead.

## Pull every number from a call that works

The calls behind the numeric sections:

```python
search(kind='job', view='count', args={'group_by': 'job_type', 'since': '7d'})
get(kind='quest', id='qu<N>', view='gaps')
get(kind='quest', id='qu<N>', view='logbook')
get(kind='gripe', id='/done', args={'closed_since': '<ISO>', 'closed_until': '<ISO>'})
get(kind='gripe', id='/open')
```

- Ticks and jobs by category, and compute and HPC: the job-count
  search, grouped by `job_type` over the week (`since` takes ages like
  `'7d'`).
- Quest state, and leads: `view='gaps'` for what blocks a quest,
  `view='logbook'` for what happened on it this week; run both per
  live quest.
- Gripes fixed: the `/done` closure window — gripes whose STATUS:done
  tag was set inside the week, newest-closed first; page through with
  the same args and confirm each handle's `resolved:` evidence before
  counting it. Do not derive fixes from the open-count delta — that
  nets out incoming, reopened and parked gripes and is not a fix count.
- Plan and issues (backlog totals): the `/open` header line — `N
  open · M older than 3d · K unpicked ≥14d · oldest grX (D days)`.

Quest views are [[precis-quest-help]]; the open-gripe queue is
[[precis-gripe-help]]; job views are [[precis-job-observe-help]].

## Make the plan a contract, and score it next week

Every item in the plan section names the handle of the dependency it
waits on — a quest (`qu42`), a gripe (`gr123456`), a todo, a job. An
item with no dependency handle is not a plan item yet.

The next report opens with a done/moved/dropped scorecard of the
previous plan (Table 0): each item scored **done** (with the evidence
handle), **moved** (with the handle it moved to), or **dropped** (with
a one-line reason). The scorecard is what keeps the plan honest; do
not skip it when the week went well.

## Hold the report to house style

- A handle in brackets on every claim: `[qu42]`, `[gr123456]`,
  `[me592803]` — no naked assertions.
- No hostnames anywhere in the report.
- Never start a sentence with a lowercase identifier — open with
  "The `<identifier>` ..." or set the identifier in `backticks` (the
  draft kind's hygiene view flags violations; see
  [[precis-draft-help]]).
- Every table that uses gate vocabulary gets a "Reading the table"
  legend — that exact phrase — explaining the columns before the data.
- Tables are numbered by hand, one sequence through the report
  (Table 0, Table 1, ...); the numbering is not automatic.
- Figures need export: a figure reaches the PDF only through an
  export (the `draft_export` job above), never a pasted or
  screenshotted image.

## See also

- [[precis-draft-help]] — the draft kind: chunks, scaffold, prose rules, hygiene.
- [[precis-draft-export-help]] — the draft_export job and what blocks it.
- [[precis-quest-help]] — quest gaps and logbook views.
- [[precis-gripe-help]] — the /done closure window, the /open queue and its header line.
- [[precis-job-observe-help]] — job views, including view='count'.
