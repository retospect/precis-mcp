---
id: precis-proposal-help
title: precis — write a proposal against a call-for-proposal
summary: ingest a call-for-proposal (kind='cfp'), seed a proposal project with the idea + personnel, link the cfp, and let the planner write the draft section-by-section, checking each section's word count against the cfp's limits
applies-to: kind='cfp' (get/search), kind='draft' (put/edit get view='wordcount'), kind='todo' (LLM:* project), link rel='has-requirement'
status: active
---

# precis-proposal-help — write a proposal, driven by the call

Writing a proposal in precis joins three pieces you already have:

1. **The call-for-proposal** — the requirements PDF, ingested as a
   `kind='cfp'` document (the *spec*). It is read-only and **never
   cited as evidence** — it tells you what to write, it is not a source
   to quote.
2. **A proposal project** — a strategic `kind='todo'` whose
   `meta.workspace.brief` holds your **idea + personnel** (free text),
   tagged `LLM:opus` so the planner drives it.
3. **The proposal draft** — a `kind='draft'` (one per project), the
   editable, chunk-native deliverable. You write **into** it.

The planner reads the call, creates one draft section per required
part, writes the prose, and self-checks each section's length against
the call's word limits.

## Intake — how a human starts one

```
# 1. Ingest the call-for-proposal (same pipeline as a paper, spec kind)
$ precis add --as cfp ~/Downloads/nsf-25-501.pdf      # → cfp slug, e.g. nsf-25-501
#    (or drop it in the watch inbox under  inbox/cfp/)
#    read it at  /cfp/<slug>  (the paper-style two-pane reader)

# 2. Create the proposal project (strategic todo) carrying the idea +
#    personnel as the workspace brief, and tag it LLM:opus so it ticks.
put(kind='todo', level='strategic',
    text='Write the NSF-25-501 proposal',
    meta={'workspace': {'path': 'projects/nsf_25_501',
                        'format': 'tex',
                        'brief': '''IDEA: <one-paragraph thesis of the proposal>.
PERSONNEL: <PI name, role, relevant track record>; <co-PI …>; <key staff …>.
CONSTRAINTS: <budget cap, duration, any eligibility notes>.'''}},
    tags=['LLM:opus'])

# 3. Link the call to the project so the planner consults it.
link(kind='todo', id=<project ref_id>, target='cfp:nsf-25-501', rel='has-requirement')

# 4. Create the draft, owned by the project (auto draft-of linked). Its
#    sections are created by the planner to match the call (below) — there
#    is no fixed proposal template; the call dictates the structure.
put(kind='draft', project=<project ref_id>,
    text='NSF-25-501 — <working title>')
```

That is all. The `dispatch` worker ticks the `LLM:opus` project; each
tick is a planner coroutine that sees a **`## Proposal requirements`**
block (the linked call's title + section headings) and the **`##
Project context`** block (your idea + personnel brief), and proceeds.

## What the planner does each tick

1. **Read the call.** `get(kind='cfp', id='<slug>', view='toc')` for the
   required sections; `get(kind='cfp', id='<slug>')` / `search(kind='cfp',
   q='word limit', scope='<slug>')` for limits, eligibility, evaluation
   criteria. Do **not** `put(kind='citation', …)` against the cfp — it is
   the spec, not evidence.
2. **Lay down the sections.** For each required part of the call, add a
   draft heading:
   `put(kind='draft', id='<draft>', chunk_kind='heading', text='<section>',
   at={'last': True})`. Stamp the section's section-style (ADR 0037) when
   the genre has one, and its **word limit** from the call:
   `edit(kind='draft', id='dc<heading>', word_target={'min':…, 'max':…})`.
3. **Write the prose.** Add paragraphs under each heading:
   `put(kind='draft', id='<draft>', chunk_kind='paragraph', text='…',
   at={'into':'dc<heading>', 'last': True})`. Cite *evidence* (papers)
   with `[§<paper>~<n>]` and the citation verb — never the cfp.
4. **Check length.** `get(kind='draft', id='<draft>', view='wordcount')`
   returns each section's word count, its target, and an over/under/ok
   verdict, plus the whole-draft total. Revise sections flagged `over`
   (trim) or `under` (expand) until every section reads `ok`. Scope to one
   section with `get(kind='draft', id='dc<heading>', view='wordcount')`.
5. **Decompose** large sections into child todos, each told to write its
   section **into the same draft** under the relevant `dc<heading>`.

## Word counts

`view='wordcount'` counts **visible prose** — paragraphs and asides —
and excludes headings, equations, figures, tables, code, and glossary
terms. Inline reference markers (`[dc…]`, `[§…]`, `[surface](¶term)`)
are stripped before counting, so a citation-dense paragraph is not
inflated by its handles. A section's count includes its subsections.
Set a target with `edit(id='dc<heading>', word_target={'min':200,'max':400})`;
clear it with `word_target={}`. The web reader shows the same total and
an amber badge when any section is off target.

## Why a CFP is its own kind (not a paper)

A `cfp` is ingested and read exactly like a `paper` — but it carries
`corpus_role='spec'`, so it stays out of `search(kind='paper', …)` and
cannot be a citation source (the citation verb resolves only
`kind='paper'`). That keeps the requirements document from ever being
quoted *as if it were evidence* in your own proposal — a category error
the separate kind makes impossible. See `precis-draft-help` (writing),
`precis-decomposition-help` (section subtasks), and `precis-tasks-help`
(projects).
